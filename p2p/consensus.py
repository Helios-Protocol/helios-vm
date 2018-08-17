import asyncio
import logging
import math
import operator
import time

from evm.utils.numeric import (
    effecient_diff,
    stake_weighted_average,
)

from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Tuple,
    Union,
    cast,
)

from cytoolz import (
    partition_all,
    unique,
)

from eth_typing import BlockNumber, Hash32
from eth_utils import (
    encode_hex,
)

from p2p.rlp import (
    BlockBody, 
    P2PTransaction,
    BlockNumberKey,
    BlockHashKey,
)

from evm.constants import (
    BLANK_ROOT_HASH, 
    EMPTY_UNCLE_HASH, 
    GENESIS_PARENT_HASH, 
    NUMBER_OF_HEAD_HASH_TO_SAVE, 
    TIME_BETWEEN_HEAD_HASH_SAVE,
)

from evm.chains import AsyncChain
from evm.db.chain import AsyncChainDB
from evm.db.trie import make_trie_root_and_nodes
from evm.exceptions import (
    HeaderNotFound, 
    NoLocalRootHashTimestamps, 
    LocalRootHashNotInConsensus
)

from evm.rlp.headers import BlockHeader
from evm.rlp.receipts import Receipt
from evm.rlp.transactions import BaseTransaction

from p2p.constants import (
    MIN_SAFE_PEERS,
    LOCAL_ROOT_HASH_CHECK_MIN_TIME_PERIOD,
    BLOCK_CONFLICT_RESOLUTION_PERIOD,
    CONSENUS_PEER_DISCONNECT_CHECK_PERIOD,
    CONSENSUS_CHECK_READY_TIME_PERIOD,
    ASK_BOOT_NODE_FOR_STAKE_CUTOFF_PERIOD,
    CONSENSUS_SYNC_TIME_PERIOD,
    CONSENSUS_CHECK_MIN_GAS_SYSTEM_READY_TIME_PERIOD,
    CONSENSUS_CHECK_LOCAL_TPC_CAP_PERIOD,
    MIN_GAS_PRICE_SYSTEM_SYNC_WITH_NETWORK_PERIOD,
    MIN_PEERS_TO_CALCULATE_NETWORK_TPC_CAP_AVG,
)

from p2p import protocol
from p2p import eth
from p2p import hls
from p2p.cancel_token import CancelToken
from p2p.exceptions import (
    NoEligiblePeers, 
    OperationCancelled,
    DatabaseResyncRequired,
)
from p2p.peer import BasePeer, HLSPeer, PeerPool, PeerPoolSubscriber
from p2p.rlp import BlockBody, P2PTransaction, TimestampRootHashKey
from p2p.service import BaseService
from p2p.utils import (
    get_process_pool_executor,
)
from sortedcontainers import SortedDict
from sortedcontainers import SortedList

#this class can just loop through this each n seconds:
    
    #request new chain head hashes
    #go through each self.conflict_blocks, and ask all connected peers which conflict block they have
    #calculate consensus
    
    #needs to be able to receive consensus related messages from other nodes during this loop
    #for example: MOVE THIS EXAMPLE TO SYNCER a node can ask us which block we have at number 5 on wallet A. Just look this up directly from db.
    
#if syncer finds a conflicting block, it can append it to conflict_blocks
#make sure this has transactions in it. they are an important part of slashing
    
#Todo. remove any conflic blocks that achieve 100% consensus



class BlockConflictInfo():
    def __init__(self, wallet_address, block_number, time_found):
        self.wallet_address = wallet_address
        self.block_number = block_number
        self.time_found = time_found
    
    
class Consensus(BaseService, PeerPoolSubscriber):
    """
    determine if items have consensus
    get items that have consensus
    """
    logger = logging.getLogger("p2p.consensus.Consensus")
    # We'll only sync if we are connected to at least min_peers_to_sync.
    min_peers_to_sync = 1
    # TODO: Instead of a fixed timeout, we should use a variable one that gets adjusted based on
    # the round-trip times from our download requests.
    _reply_timeout = 60
    _is_bootnode = None
    _local_root_hash_timestamps = None
    _min_gas_system_ready = False
    #this is {peer_wallet_address: [timestamp_received, network_tpc_cap, stake]}
    _network_tpc_cap_statistics = {}
    _last_check_local_tpc_cap_time = 0
    _local_tpc_cap = 0
    _last_check_if_min_gas_system_ready_time = 0
    
    def __init__(self,
                 chain: AsyncChain,
                 chaindb: AsyncChainDB,
                 base_db,
                 peer_pool: PeerPool,
                 chain_head_db,
                 bootstrap_nodes,
                 chain_config,
                 token: CancelToken = None) -> None:
        super().__init__(token)
        self.chain = chain
        self.chaindb = chaindb
        self.base_db = base_db
        self.chain_head_db = chain_head_db
        self.peer_pool = peer_pool
        self.chain_config = chain_config
        self.bootstrap_nodes = bootstrap_nodes
        self._executor = get_process_pool_executor()
        #[BlockConflictInfo, BlockConflictInfo, ...]
        self.block_conflicts = set()
        #dont forget to include children blocks into weight
        #{peer_wallet_address, [peer_stake, [hls.BlockHashKey]]}
        self.peer_block_choices = {}
        #{chain_wallet_address, {block_number, (block_hash, stake)}}
        self.block_choice_consensus = {}
        #TODO: this might become very slow if someone makes a huge number of conflict blocks. need to worry about that.
        #{chain_wallet_address, {block_number, {block_hash, total_stake}}
        self.block_choice_statistics = {}
        
        #{peer_wallet_address, [peer_stake, [[timestamp, root_hash],[timestamp, root_hash]...]]}
        self.peer_root_hash_timestamps = {}
        #{timestamp, (root_hash, stake)}
        self.root_hash_timestamps_consensus = {}
        #{timestamp, {root_hash, total_stake}}
        self.root_hash_timestamps_statistics = {}
        
        #[peer_wallet_address, stake, [hls.BlockHashKey, hls.BlockHashKey, ...]
        self._new_peer_block_choices = asyncio.Queue()
        #[peer_wallet_address, stake, [[timestamp, root_hash],[timestamp, root_hash]...]]
        self._new_peer_chain_head_root_hash_timestamps = asyncio.Queue()
        
        
        self._last_send_sync_message_time = 0
        self._last_block_choice_consensus_calculation_time = 0
        self._last_get_stake_from_bootnode_time = 0
        self._last_check_if_syncing_time = 0
        self._last_check_to_remove_blocks_that_acheived_consensus = 0
        self._last_check_to_remove_disconnected_peer_data = 0
        self._last_check_to_see_if_consensus_ready = 0
        self._last_check_local_root_hash_timestamps = 0
        self._last_check_to_remove_old_local_root_hash_timestamps_from_peer_statistics = 0
        
        #{wallet_address:stake...}
        self.peer_stake_from_bootstrap_node = {}
        
        self.num_peers_contributing_to_consensus = 0
     
        self._is_syncing = True
        
        
        self.coro_is_ready = asyncio.Event()
     
    '''
    Properties and utils
    '''
    #TODO. check to make sure the peers also have stake that is not equal to None
    @property
    def has_enough_peers(self):
        if len(self.peer_pool.connected_nodes) >= MIN_SAFE_PEERS:
            self.logger.debug("Has enough peers. connected peers: {}".format(self.peer_pool.connected_nodes.keys()))
        else:
            #self.logger.debug("doesnt have enough peers. connected peers: {}".format(self.peer_pool.connected_nodes.keys()))
            pass
        return len(self.peer_pool.connected_nodes) >= MIN_SAFE_PEERS
    
    @property
    def is_bootnode(self):
        if self._is_bootnode is None:
            self._is_bootnode = self.chain_config.nodekey_public in [x.pubkey for x in self.chain_config.bootstrap_nodes]
        return self._is_bootnode
    @property
    def has_enough_consensus_participants(self):
        if len(self.peer_root_hash_timestamps) >= MIN_SAFE_PEERS or self.is_bootnode:
            self.logger.debug("has_enough_consensus_participants. wallets involved: {}".format(self.peer_root_hash_timestamps.keys()))
        else:
            #self.logger.debug("doesnt has_enough_consensus_participants. wallets involved: {}".format(self.peer_root_hash_timestamps.keys()))
            pass
        return len(self.peer_root_hash_timestamps) >= MIN_SAFE_PEERS or self.is_bootnode
    
    @property
    async def is_syncing(self):
        '''
        This determines if our local blockchain database is still syncing. 
        If this is the case, then we cannot trust the stake we have here, 
        and we temporarily give all peers equal stake
        '''
        #1) if our newest root_hash timestamp is older than 1000*1000 seconds, we are syncing
        #this can be the only requirement. Therefore, we must make sure that we don't ever save the 
        #root hash timestamp unless sync is complete. So we cannot do a normal import until sync is complete
        if self._last_check_if_syncing_time < (int(time.time()) - CONSENSUS_SYNC_TIME_PERIOD):
            local_root_hash_timestamps = await self.chain_head_db.coro_get_historical_root_hashes(after_timestamp=(int(time.time())-ASK_BOOT_NODE_FOR_STAKE_CUTOFF_PERIOD))
            if local_root_hash_timestamps == None:
                self._is_syncing = True
            else:
                self._is_syncing = False
        
            #self.logger.debug("IS_SYNCING: {}, local_root_hash_timestamps: {}".format(self._is_syncing, local_root_hash_timestamps))
        return self._is_syncing
    
    @property 
    def min_gas_system_ready(self):
        '''
        checks to see if the throttling system that controls minimum required gas is ready. 
        if it is not ready we cannot accept new blocks unless from another node.
        '''
        if self._min_gas_system_ready is False or self._last_check_if_min_gas_system_ready_time < (int(time.time()) - CONSENSUS_CHECK_MIN_GAS_SYSTEM_READY_TIME_PERIOD):
            self._min_gas_system_ready = not self.chaindb.min_gas_system_initialization_required()
            self._last_check_if_min_gas_system_ready_time = int(time.time())
        
        return self._min_gas_system_ready
        
    @property
    async def local_tpc_cap(self):
        '''
        the maximum number of blocks that can be imported with a single transaction in 1 centisecond.
        not exactly the tpc, but something consistent to base throttling off of.
        '''
        if self._local_tpc_cap == 0 or self._last_check_local_tpc_cap_time < (int(time.time()) - CONSENSUS_CHECK_LOCAL_TPC_CAP_PERIOD):
            self._local_tpc_cap = await self.chain.coro_get_local_tpc_cap()
            self._last_check_local_tpc_cap_time = int(time.time())
        
        return self._local_tpc_cap
    
        
    async def get_accurate_stake(self, wallet_address, local_stake):
        if local_stake == 0 or await self.is_syncing:
            try:
                return self.peer_stake_from_bootstrap_node[wallet_address]
            except KeyError:
                return local_stake
        return local_stake  

 
    def determine_stake_winner(self, item_stakes_dict_or_list):
        '''
        takes in a dictionary where the keys are the items which are voted on, and the values are the stake.
        returns a tuple containing the highest stake item, and its stake
        '''
        if isinstance(item_stakes_dict_or_list, dict):
            max_stake = 0
            max_item = None
            for item, stake in item_stakes_dict_or_list.items():
                if stake > max_stake:
                    max_stake = stake
                    max_item = item
                elif stake == max_stake:
                    if max_item == None:
                        max_stake = stake
                        max_item = item
                    else:
                        if item > max_item:
                            max_stake = stake
                            max_item = item
            assert(max_item is not None)
            return (max_item, stake)      
        elif isinstance(item_stakes_dict_or_list, list):
            max_stake = 0
            max_item = None
            for item_stake in item_stakes_dict_or_list:
                item = item_stake[0]
                stake = item_stake[1]
                if stake > max_stake:
                    max_stake = stake
                    max_item = item
                elif stake == max_stake:
                    if max_item == None:
                        max_stake = stake
                        max_item = item
                    else:
                        if item > max_item:
                            max_stake = stake
                            max_item = item
            assert(max_item is not None)
            return (max_item, stake)   
    
    def calc_stake_difference(self, prev_block_hash_keys, new_block_hash_keys):
        '''
        effeciently determines the difference between the new data and the old data.
        Returns stake_subtract, which is items there previously that are no longer there
        and stake add, which are new items that werent there previously.
        '''
        stake_subtract = effecient_diff(new_block_hash_keys, prev_block_hash_keys)
        stake_add = effecient_diff(prev_block_hash_keys, new_block_hash_keys)
        return stake_subtract, stake_add
        
    def delta_block_choice_statistics(self, chain_wallet_address, block_number, block_hash, delta):
        '''
        adds or subtracts stake for a given block choice. Stores the new statistics in local statistics files
        '''
        try:
            self.block_choice_statistics[chain_wallet_address][block_number][block_hash] += delta
        except KeyError:
            if chain_wallet_address in self.block_choice_statistics:
                if block_number in self.block_choice_statistics[chain_wallet_address]:
                    self.block_choice_statistics[chain_wallet_address][block_number][block_hash] = delta
                else:
                    self.block_choice_statistics[chain_wallet_address][block_number] = {block_hash: delta}
            else:
                self.block_choice_statistics[chain_wallet_address] = {block_number: {block_hash: delta}}
        
    def delta_root_hash_timestamp_statistics(self, timestamp, root_hash, delta):
        '''
        adds or subtracts stake for a given root hash. Stores the new statistics in local statistics files
        '''
        try:
            self.root_hash_timestamps_statistics[timestamp][root_hash] += delta
        except KeyError:
            if timestamp in self.root_hash_timestamps_statistics:
                self.root_hash_timestamps_statistics[timestamp][root_hash] = delta
            else:
                self.root_hash_timestamps_statistics[timestamp] = {root_hash: delta}
    
    def get_winner_stake_binary_compare(self, bin_item_1, stake_1, bin_item_2, stake_2):
        '''
        Returns the item of largest stake, it there is a tie, it compares the binary items and returns the greatest
        '''
        if stake_1 > stake_2:
            #our stake is greater, lets stick with our choice.
            return bin_item_1
        elif stake_1 < stake_2:
            #peers have more stake. lets go with their choice.
            return bin_item_2
        else:
            #we have a tie. return the greater binary hash
            if bin_item_1 > bin_item_2:
                return bin_item_1
            else:
                return bin_item_2
                
            
    '''
    Standard service functions
    '''
    def register_peer(self, peer: BasePeer) -> None:
        #self.peer_root_hash_timestamps[peer.wallet_address] = [new_peer_stake, new_root_hash_timestamps]
        pass
        
    async def _handle_msg_loop(self) -> None:
        while self.is_running:
            try:
                peer, cmd, msg = await self.wait(self.msg_queue.get())
            except OperationCancelled:
                break

            # Our handle_msg() method runs cpu-intensive tasks in sub-processes so that the main
            # loop can keep processing msgs, and that's why we use ensure_future() instead of
            # awaiting for it to finish here.
            #self.logger.debug("received cmd, msg {}, {}".format(cmd, msg))
            asyncio.ensure_future(self.handle_msg(peer, cmd, msg))

    async def handle_msg(self, peer: HLSPeer, cmd: protocol.Command,
                         msg: protocol._DecodedMsgType) -> None:
        try:
            await self._handle_msg(peer, cmd, msg)
        except OperationCancelled:
            # Silently swallow OperationCancelled exceptions because we run unsupervised (i.e.
            # with ensure_future()). Our caller will also get an OperationCancelled anyway, and
            # there it will be handled.
            pass
        except Exception:
            self.logger.exception("Unexpected error when processing msg from %s", peer)

    async def _run(self) -> None:
        asyncio.ensure_future(self._handle_msg_loop())
        test = asyncio.ensure_future(self.sync_min_gas_price_system())
        #first lets make sure we add our own root_hash_timestamps
        with self.subscribe(self.peer_pool):
            while True:
                
                if self.coro_is_ready.is_set():
                    ready = 'true'
                else:
                    ready = 'false'
                self.logger.debug("self.coro_is_ready = {}".format(ready))
                #first lets ask the bootnode for the stake of any peers that we dont have the blockchain for
                #this takes care of determining stake of our peers while we are still syncing our blockchain database
                self.logger.debug("waiting for get missing stake from bootnode")
                await self.get_missing_stake_from_bootnode()
                #send sync messages, doesnt need to be async
                self.send_sync_get_messages()
                #it will re-loop every time it gets a new response from a single peer. this ensures that the statistics are always as up to date as possible
                self.logger.debug("waiting for receive_sync_messages")
                await self.receive_sync_messages()
                
                #self.logger.debug("done syncing consensus. These are the statistics for block_choices, root_hashes: {0}, {1}".format(
                #                    self.block_choice_statistics, 
                #                    self.root_hash_timestamps_statistics))
                
                
                
                test_1 = self.chaindb.load_historical_network_tpc_capability()
                test_2 = self.chaindb.load_historical_minimum_gas_price()
                test_3 = self.chaindb.load_historical_tx_per_centisecond()
                self.logger.debug("net_tpc_cap, min_gas_price, tpc = {}, {}, {}".format(test_1, test_2, test_3))
                
                #here we shouldnt pause because if it returned early than thats because we got some data from peers. we want to process data asap.
                #self.populate_peer_consensus()
                #TODO. when a peer disconnects, make sure we delete their vote.
                
                self.remove_data_for_old_root_hash_timestamps()
                self.remove_data_for_disconnected_peers()
                self.remove_data_for_blocks_that_achieved_consensus()
                #this is run after populate consensus. so if there are enough peers who we have root hashes for, then they will be included in consensus.
                self.check_if_ready()
                
                #yeild control to other coroutines after each loop
                #await asyncio.sleep(0)
            
                
    async def _cleanup(self) -> None:
        # We don't need to cancel() anything, but we yield control just so that the coroutines we
        # run in the background notice the cancel token has been triggered and return.
        await asyncio.sleep(0)
        
    def check_if_ready(self):
        if self._last_check_to_see_if_consensus_ready < (int(time.time()) - CONSENSUS_CHECK_READY_TIME_PERIOD):
            if self.has_enough_consensus_participants and self.has_enough_peers:
                #await asyncio.sleep()
                
                self.coro_is_ready.set()
            else:
                self.coro_is_ready.clear()
            self._last_check_to_see_if_consensus_ready = int(time.time())
    '''
    Core functionality
    '''
    def send_sync_get_messages(self) -> None:
        if self._last_send_sync_message_time < (int(time.time()) - CONSENSUS_SYNC_TIME_PERIOD):
            #sync peer block choices and chain head root hashes
            self.logger.info("Sending syncing consensus messages to all connected peers")
            
            block_number_keys = []
            for conflict in self.block_conflicts:
                block_number_keys.append(BlockNumberKey(wallet_address = conflict.wallet_address, block_number = conflict.block_number))
                
            for peer in self.peer_pool.peers:
                #send message , and log time that message was sent. Be sure not to send a message to any node that we have a pending response for
                #TODO: delete pending responses longer than 60 seconds, and resend.
                if len(block_number_keys) > 0:
                    peer.sub_proto.send_get_unordered_block_header_hash(block_number_keys)
                peer.sub_proto.send_get_chain_head_root_hash_timestamps(0)
                
            self._last_send_sync_message_time = int(time.time())
        
    
    async def receive_sync_messages(self):
        try:
            block_choices_or_chain_head_root_hash_timestamps = await self.wait_first(
                self._new_peer_block_choices.get(),
                self._new_peer_chain_head_root_hash_timestamps.get(),
                token=self.cancel_token,
                timeout=CONSENSUS_SYNC_TIME_PERIOD)
        except TimeoutError:
            self.logger.warn("Timeout waiting for block choices or chain head root hash timestamps")
            return
        
        
        #here we need to check the instance to determine which it is.
        #we will also be receiving blocks that syncer requests, so make sure we check that it is one of the blocks we asked for.
        if isinstance(block_choices_or_chain_head_root_hash_timestamps, hls.UnorderedBlockHeaderHash):
            block_choices = block_choices_or_chain_head_root_hash_timestamps
            peer_wallet_address = block_choices[0]
            new_peer_stake = block_choices[1]
            new_block_hash_keys = block_choices[2]
            
            #lets only update diff for this peer to reduce overhead.
            if peer_wallet_address in self.peer_block_choices:
                previous_peer_stake = self.peer_block_choices[peer_wallet_address][0]
                previous_block_hash_keys = self.peer_block_choices[peer_wallet_address][1]
                
                #lets just find the difference this way. should be more effectient. hopefully.
                stake_sub, stake_add = self.calc_stake_difference(previous_block_hash_keys, new_block_hash_keys)
                #first we subtract the previous stake
                for previous_block_hash_key in stake_sub:
                    self.delta_block_choice_statistics(previous_block_hash_key.wallet_address,
                                                       previous_block_hash_key.block_number,
                                                       previous_block_hash_key.block_hash,
                                                       -1*previous_peer_stake)
         
                #now add the new stake with new choices
                for new_block_hash_key in stake_add:
                    self.delta_block_choice_statistics(new_block_hash_key.wallet_address,
                                                       new_block_hash_key.block_number,
                                                       new_block_hash_key.block_hash,
                                                       new_peer_stake)
                
#                #first we subtract the previous stake
#                for previous_block_hash_key in previous_block_hash_keys:
#                    self.delta_block_choice_statistics(previous_block_hash_key.wallet_address,
#                                                       previous_block_hash_key.block_number,
#                                                       previous_block_hash_key.block_hash,
#                                                       -1*previous_peer_stake)
#         
#                #now add the new stake with new choices
#                for new_block_hash_key in new_block_hash_keys:
#                    self.delta_block_choice_statistics(new_block_hash_key.wallet_address,
#                                                       new_block_hash_key.block_number,
#                                                       new_block_hash_key.block_hash,
#                                                       new_peer_stake)
                
            else:
                #this is the first message from them, we don't have any previous choices, so lets just add the new stake
                for new_block_hash_key in new_block_hash_keys:
                    self.delta_block_choice_statistics(new_block_hash_key.wallet_address,
                                                       new_block_hash_key.block_number,
                                                       new_block_hash_key.block_hash,
                                                       new_peer_stake)
                    
            #finally, update the peer block choices
            self.peer_block_choices[peer_wallet_address] = [new_peer_stake, new_block_hash_keys]
            
            #TODO: calculate consensus, and remove all data for anything that has reached consensus.
        else:
            root_hash_timestamp_msg = block_choices_or_chain_head_root_hash_timestamps
            peer_wallet_address = root_hash_timestamp_msg[0]
            new_peer_stake = root_hash_timestamp_msg[1]
            new_root_hash_timestamps = root_hash_timestamp_msg[2]
            #self.logger.debug("dealing with new root_hash_timestamps {}".format(new_root_hash_timestamps))
            
            #first we check to see if we have an entry for this peer:
            if peer_wallet_address in self.peer_root_hash_timestamps:
                previous_peer_stake = self.peer_root_hash_timestamps[peer_wallet_address][0]
                previous_root_hash_timestamps = self.peer_root_hash_timestamps[peer_wallet_address][1]
                
                #lets just find the difference this way. should be more effectient. hopefully.
                stake_sub, stake_add = self.calc_stake_difference(previous_root_hash_timestamps, new_root_hash_timestamps)
                
                #self.logger.debug("subtracting stake {} from timestamps {}".format(previous_peer_stake, [x[0] for x in stake_sub]))
                #self.logger.debug("adding stake {} from timestamps {}".format(new_peer_stake, [x[0] for x in stake_add]))
                #first we subtract the previous stake
                for previous_root_hash_timestamp in stake_sub:
                    self.delta_root_hash_timestamp_statistics(
                                                       previous_root_hash_timestamp[0], #timestamp
                                                       previous_root_hash_timestamp[1], #root_hash
                                                       -1*previous_peer_stake)
         
                #now add the new stake with new choices
                for new_root_hash_timestamp in stake_add:
                    self.delta_root_hash_timestamp_statistics(
                                                       new_root_hash_timestamp[0], #timestamp
                                                       new_root_hash_timestamp[1], #root_hash
                                                       new_peer_stake)
            else:
                #now add the new stake with new choices
                for new_root_hash_timestamp in new_root_hash_timestamps:
                    self.delta_root_hash_timestamp_statistics(
                                                       new_root_hash_timestamp[0], #timestamp
                                                       new_root_hash_timestamp[1], #root_hash
                                                       new_peer_stake)
            #finally, update the peer block choices
            self.peer_root_hash_timestamps[peer_wallet_address] = [new_peer_stake, new_root_hash_timestamps]   
                
            
    async def calculate_average_network_tpc_cap(self):
        num_candidates = 0
        if len(self._network_tpc_cap_statistics) >= MIN_PEERS_TO_CALCULATE_NETWORK_TPC_CAP_AVG:
            all_candidate_item_stake = []
            
            for wallet_address, timestamp_max_tpc_cap_stake in self._network_tpc_cap_statistics.copy().items():
                if timestamp_max_tpc_cap_stake[0] >= int(time.time())-5*60:
                    all_candidate_item_stake.append([timestamp_max_tpc_cap_stake[1], timestamp_max_tpc_cap_stake[2]])
                    num_candidates +=1
                else:
                    del(self._network_tpc_cap_statistics[wallet_address])
        if num_candidates >= MIN_PEERS_TO_CALCULATE_NETWORK_TPC_CAP_AVG:
            #add in our local tpc and stake
            local_tpc_cap = await self.local_tpc_cap
            local_stake = await self.chain.coro_get_mature_stake(self.chain_config.node_wallet_address)
            
            all_candidate_item_stake.append([local_tpc_cap, local_stake])
            
            average_network_tpc_cap = int(stake_weighted_average(all_candidate_item_stake))
            return average_network_tpc_cap
        else:
            return None
            
    async def sync_min_gas_price_system(self):
        '''
        Makes sure our system for keeping track of minimum allowed gas price is in sync with the network
        This is used to throttle the transaction rate when it reaches the limit that the network can handle.
        '''
        while True:
            if self.min_gas_system_ready:
                self.logger.debug("sync_min_gas_price_system, min_gas_system_ready = True")
                average_network_tpc_cap = await self.calculate_average_network_tpc_cap()
                if average_network_tpc_cap is not None:
                    self.chain.update_current_network_tpc_capability(average_network_tpc_cap, update_min_gas_price = True)
                
                #TODO. here we just ask for the last centisecond.
                for peer in self.peer_pool.peers:
                    peer.sub_proto.send_get_min_gas_parameters(num_centiseconds_from_now=0)
                
            else:
                self.logger.debug("sync_min_gas_price_system, min_gas_system_ready = False")
                #here we just ask for the last 50 centiseconds.
                await self.initialize_min_gas_price_from_bootnode_if_required()
            await asyncio.sleep(MIN_GAS_PRICE_SYSTEM_SYNC_WITH_NETWORK_PERIOD)
        
    async def initialize_min_gas_price_from_bootnode_if_required(self):
        if not self.min_gas_system_ready:
            for boot_node in self.bootstrap_nodes:
                try: 
                    boot_node_peer = self.peer_pool.connected_nodes[boot_node]
                    #lets just ask the first bootnode we find that we are connected to.
                    boot_node_peer.sub_proto.send_get_min_gas_parameters(num_centiseconds_from_now=50)
                    return
                except KeyError:
                    pass
            
        
    async def get_missing_stake_from_bootnode(self):
        if self._last_get_stake_from_bootnode_time < (int(time.time()) - CONSENSUS_SYNC_TIME_PERIOD):
            await self._get_missing_stake_from_bootnode()
            self._last_get_stake_from_bootnode_time = int(time.time())
       
    async def _get_missing_stake_from_bootnode(self):
        addresses_needing_stake = []
        if await self.is_syncing:
            #in this case, lets ask for the stake of all peers because ours is inaccurate
            for peer in self.peer_pool.peers:
                #if they already exist in peer_stake_from_bootstrap_node, then we already received a reply from the bootnode.
                #since this is only nessisary while sincing, it should be a short time ad the stake should still be accurate.
                #no need to refresh.
                if peer.wallet_address not in self.peer_stake_from_bootstrap_node:
                    addresses_needing_stake.append(peer.wallet_address)
                        
        else:
        
            for peer in self.peer_pool.peers:
                if peer.stake == 0:
                    if peer.wallet_address not in self.peer_stake_from_bootstrap_node:
                        addresses_needing_stake.append(peer.wallet_address)
                        
        if addresses_needing_stake == []:
            return
        
        for boot_node in self.bootstrap_nodes:
            try: 
                boot_node_peer = self.peer_pool.connected_nodes[boot_node]
                #lets just ask the first bootnode we find that we are connected to.
                boot_node_peer.sub_proto.send_get_stake_for_addresses(addresses_needing_stake)
                return
            except KeyError:
                pass
            
        #if it gets to here, then we arent connected to any boot nodes. Or we have no bootnodes. Nothing we can do right now. Try again later.
               
                
        
        
    def populate_peer_consensus(self):
        if self._last_block_choice_consensus_calculation_time < (int(time.time()) - CONSENSUS_SYNC_TIME_PERIOD):
            self._populate_peer_consensus()
            self._last_block_choice_consensus_calculation_time = int(time.time())
            
    def _populate_peer_consensus(self):
        '''
        Populates local consensus variables with the given consensus of the particular type. Doesnt account for local chain data
        '''
        #first we calculate consensus on state root timestamps
#        self.root_hash_timestamps_consensus = {}
#        oldest_allowed_time = int(time.time()) - (NUMBER_OF_HEAD_HASH_TO_SAVE)*TIME_BETWEEN_HEAD_HASH_SAVE*2
#        for timestamp, root_hash_stakes in self.root_hash_timestamps_statistics.copy().items():
#            if timestamp < oldest_allowed_time:
#                del(self.root_hash_timestamps_statistics[timestamp])
#            else:
#                self.root_hash_timestamps_consensus[timestamp] = self.determine_stake_winner(root_hash_stakes)
#                    
        
#        #now we calculate the same for conflict blocks
#        self.block_choice_consensus = {}
#        for chain_wallet_address, block_numbers in self.block_choice_statistics.items():
#            block_number_consensus = {}
#            for block_number, block_hash_stakes in block_numbers.items():
#                block_number_consensus[block_number] = self.determine_stake_winner(block_hash_stakes)
#            self.block_choice_consensus[chain_wallet_address] = block_number_consensus
#            
#   
    def remove_data_for_old_root_hash_timestamps(self):
        if self._last_check_to_remove_old_local_root_hash_timestamps_from_peer_statistics < (int(time.time()) - CONSENSUS_SYNC_TIME_PERIOD):
            self._remove_data_for_old_root_hash_timestamps()
            self._last_check_to_remove_old_local_root_hash_timestamps_from_peer_statistics = int(time.time())
            
    def _remove_data_for_old_root_hash_timestamps(self):
        oldest_allowed_time = int(time.time()) - (NUMBER_OF_HEAD_HASH_TO_SAVE)*TIME_BETWEEN_HEAD_HASH_SAVE*2
        for timestamp, root_hash_stakes in self.root_hash_timestamps_statistics.copy().items():
            if timestamp < oldest_allowed_time:
                del(self.root_hash_timestamps_statistics[timestamp])


    def remove_data_for_blocks_that_achieved_consensus(self):
        if self._last_check_to_remove_blocks_that_acheived_consensus < (int(time.time()) - CONSENSUS_SYNC_TIME_PERIOD):
            self._remove_data_for_blocks_that_achieved_consensus()
            self._last_check_to_remove_blocks_that_acheived_consensus = int(time.time())
    
    #TODO. make sure we have the consensus block before deleting.
    #we should actually just combine this with a script that syncs our database with consensus
    def _remove_data_for_blocks_that_achieved_consensus(self):
        for block_conflict_info in self.block_conflicts.copy():
            if block_conflict_info.time_found < (int(time.time()) - BLOCK_CONFLICT_RESOLUTION_PERIOD):
                if len(self.block_choice_statistics[block_conflict_info.wallet_address][block_conflict_info.block_number]) == 1:
                    #we have 100% consensus. Lets delete the conflictblock and accompanying data
                    del(self.block_choice_statistics[block_conflict_info.wallet_address][block_conflict_info.block_number])
                    self.block_conflicts.remove(block_conflict_info)
                    del(self.block_choice_consensus[block_conflict_info.wallet_address][block_conflict_info.block_number])
      
    def remove_data_for_disconnected_peers(self):
         if self._last_check_to_remove_disconnected_peer_data < (int(time.time()) - CONSENUS_PEER_DISCONNECT_CHECK_PERIOD):
            self._remove_data_for_disconnected_peers()
            self._last_check_to_remove_disconnected_peer_data = int(time.time())
    
    def _remove_data_for_disconnected_peers(self):
        connected_peer_wallet_addresses = set()
        for peer in self.peer_pool.peers:
            connected_peer_wallet_addresses.add(peer.wallet_address)
            
        for wallet_address in self.peer_root_hash_timestamps.copy().keys():
            if wallet_address not in connected_peer_wallet_addresses:
                self.logger.debug("removing root_hash_timestamps for peer {} because they have disconnected".format(wallet_address))
                del self.peer_root_hash_timestamps[wallet_address]
                

        for wallet_address in self.peer_block_choices.copy().keys():
            if wallet_address not in connected_peer_wallet_addresses:
                self.logger.debug("removing block_choices for peer {} because they have disconnected".format(wallet_address))
                del(self.peer_block_choices[wallet_address])
        
             
        
    '''
    Consensus API
    '''
    def add_block_conflict(self, chain_wallet_address, block_number):
        '''
        When a conflict block is found, add it to the consensus check using this function
        '''
        self.block_conflicts.add(BlockConflictInfo(chain_wallet_address, block_number, int(time.time())))
        
    async def get_closest_root_hash_consensus(self, timestamp):
        '''
        Returns the closest timestamp that we have a saved root hash for
        '''
        sorted_root_hash_timestamps = reversed(SortedDict(self.root_hash_timestamps_statistics))
        #goes from greatest to smallest
        for available_timestamp in sorted_root_hash_timestamps.keys():
            if available_timestamp <= timestamp:
                to_return =  available_timestamp, await self.coro_get_root_hash_consensus(available_timestamp)
                return to_return
        
        if self.is_bootnode:
            self.logger.debug("using local root hash timestamps for get_closest_root_hash_consensus because am bootnode")
            local_root_hash_timestamps = self.local_root_hash_timestamps
            sorted_local_root_hash_timestamps = SortedDict(lambda x: int(x)*-1, local_root_hash_timestamps)
            for available_timestamp, root_hash in sorted_local_root_hash_timestamps.items():
                if available_timestamp <= timestamp:
                    to_return =  available_timestamp, root_hash
                    return to_return
            
            
        return None, None
    
    
    def get_next_consensus_root_hash_after_timestamp_that_differs_from_local_at_timestamp(self, timestamp):
        '''
        Returns the next consensus root hash that differs from our local root hash at the given timestamp
        '''
        initial_local_root_hash_at_timestamp = self.local_root_hash_timestamps[timestamp]
        #self.logger.debug("initial root hash = {}".format(initial_local_root_hash_at_timestamp))
        #self.logger.debug("consensus root hash at initial timestamp = {}".format(self.get_root_hash_consensus(timestamp)))
        sorted_root_hash_timestamps = SortedDict(self.root_hash_timestamps_statistics)
        #goes from smallest to greatest
        for available_timestamp in sorted_root_hash_timestamps.keys():
            if available_timestamp > timestamp:
                to_return =  available_timestamp, self.get_root_hash_consensus(available_timestamp)
                if to_return[1] != initial_local_root_hash_at_timestamp:
                    return to_return
        
        if self.is_bootnode:
            self.logger.debug("using local root hash timestamps for get_next_consensus_root_hash_after_timestamp because am bootnode")
            local_root_hash_timestamps = self.local_root_hash_timestamps
            sorted_local_root_hash_timestamps = SortedDict(local_root_hash_timestamps)
            for available_timestamp, root_hash in sorted_local_root_hash_timestamps.items():
                if available_timestamp > timestamp:
                    to_return =  available_timestamp, root_hash
                    if to_return[1] != initial_local_root_hash_at_timestamp:
                        return to_return
                    
        return None, None
    
    def get_next_consensus_root_hash_after_timestamp(self, timestamp):
        '''
        Returns the next consensus root hash that differs from our local root hash at the given timestamp
        '''
        #initial_local_root_hash_at_timestamp = self.local_root_hash_timestamps[timestamp]
        #self.logger.debug("initial root hash = {}".format(initial_local_root_hash_at_timestamp))
        #self.logger.debug("consensus root hash at initial timestamp = {}".format(self.get_root_hash_consensus(timestamp)))
        sorted_root_hash_timestamps = SortedDict(self.root_hash_timestamps_statistics)
        #goes from smallest to greatest
        for available_timestamp in sorted_root_hash_timestamps.keys():
            if available_timestamp > timestamp:
                to_return =  available_timestamp, self.get_root_hash_consensus(available_timestamp)
                return to_return
        
        if self.is_bootnode:
            self.logger.debug("using local root hash timestamps for get_next_consensus_root_hash_after_timestamp because am bootnode")
            local_root_hash_timestamps = self.local_root_hash_timestamps
            sorted_local_root_hash_timestamps = SortedDict(local_root_hash_timestamps)
            for available_timestamp, root_hash in sorted_local_root_hash_timestamps.items():
                if available_timestamp > timestamp:
                    to_return =  available_timestamp, root_hash
                    return to_return
                    
        return None, None
        
        
        
    
    def get_newest_peer_root_hash_timestamp(self):
        return list(SortedDict(self.root_hash_timestamps_statistics).keys())[-1]
        
    
    @property
    def local_root_hash_timestamps(self):
        local_root_hash_timestamps = self.chain_head_db.get_historical_root_hashes()
        
        if local_root_hash_timestamps is not None:
            self._local_root_hash_timestamps = dict(local_root_hash_timestamps)
        else:
            self._local_root_hash_timestamps = None
        
        return self._local_root_hash_timestamps
    
    async def coro_get_root_hash_consensus(self, timestamp, local_root_hash_timestamps = None):
        '''
        Returns the consensus root hash for a given timestamp
        '''
        
        if local_root_hash_timestamps is None:
            local_root_hash_timestamps = self.local_root_hash_timestamps
            
        if local_root_hash_timestamps is not None:
            try:
                local_root_hash = local_root_hash_timestamps[timestamp]
            except KeyError:
                local_root_hash = None
        else:
            local_root_hash = None
           
        
        try:
            root_hash_stakes = self.root_hash_timestamps_statistics[timestamp]
            peer_root_hash, peer_stake_for_peer_root_hash = self.determine_stake_winner(root_hash_stakes)
        except KeyError:
            peer_root_hash = None
            
        if peer_root_hash is None:
            return local_root_hash
        
        elif peer_root_hash == local_root_hash or local_root_hash is None:
            return peer_root_hash
        else:
            our_stake_for_local_hash = await self.chain.coro_get_mature_stake(self.chain_config.node_wallet_address)
            try:
                peer_stake_for_local_hash = self.root_hash_timestamps_statistics[timestamp][local_root_hash]
            except KeyError:
                peer_stake_for_local_hash = 0
            total_stake_for_local_hash = our_stake_for_local_hash + peer_stake_for_local_hash
            
            return self.get_winner_stake_binary_compare(peer_root_hash, 
                                                        peer_stake_for_peer_root_hash, 
                                                        local_root_hash, 
                                                        total_stake_for_local_hash)
            
    def get_root_hash_consensus(self, timestamp, local_root_hash_timestamps = None):
        '''
        Returns the consensus root hash for a given timestamp
        '''
        if local_root_hash_timestamps is None:
            local_root_hash_timestamps = self.local_root_hash_timestamps
        if local_root_hash_timestamps is not None:
            try:
                local_root_hash = local_root_hash_timestamps[timestamp]
            except KeyError:
                local_root_hash = None
        else:
            local_root_hash = None
           
        
        try:
            root_hash_stakes = self.root_hash_timestamps_statistics[timestamp]
            peer_root_hash, peer_stake_for_peer_root_hash = self.determine_stake_winner(root_hash_stakes)
        except KeyError:
            peer_root_hash = None
            
        if peer_root_hash is None:
            return local_root_hash
        
        elif peer_root_hash == local_root_hash or local_root_hash is None:
            return peer_root_hash
        else:
            our_stake_for_local_hash = self.chain.get_mature_stake(self.chain_config.node_wallet_address)
            try:
                peer_stake_for_local_hash = self.root_hash_timestamps_statistics[timestamp][local_root_hash]
            except KeyError:
                peer_stake_for_local_hash = 0
            total_stake_for_local_hash = our_stake_for_local_hash + peer_stake_for_local_hash
            
            return self.get_winner_stake_binary_compare(peer_root_hash, 
                                                        peer_stake_for_peer_root_hash, 
                                                        local_root_hash, 
                                                        total_stake_for_local_hash)
        
        
               
    async def get_latest_root_hash_before_conflict(self, before_timestamp = None):
        '''
        If one of our root hash timestamps is in conflict with consensus, then this returns the root hash and timestamp for the 
        latest one before that where we were still in consensus
        
        if before_timestamp is set, it only looks at root hashes with timestamp earlier than before_timestamp
        '''
        local_root_hash_timestamps = self.local_root_hash_timestamps
        if local_root_hash_timestamps is None:
            raise NoLocalRootHashTimestamps()
           
        sorted_local_root_hash_timestamps = SortedDict(lambda x: int(x)*-1, local_root_hash_timestamps)
        #sorted_local_root_hash_timestamps = SortedDict(local_root_hash_timestamps)
         
        disagreement_found = False
        #it now goes from newest to oldest
        for timestamp, local_root_hash in sorted_local_root_hash_timestamps.items():
            if before_timestamp is not None:
                if timestamp > before_timestamp:
                    continue
            consensus_root_hash = await self.coro_get_root_hash_consensus(timestamp, local_root_hash_timestamps = local_root_hash_timestamps)
            
            if local_root_hash == consensus_root_hash:
                if disagreement_found:
                    #this is the first agreeing one after some disagreeing ones. This is what we return
                    return local_root_hash, timestamp
                else:
                    #we are in agreemenet from the newest roothash without any disagreements, we break and return none
                    break
            else:
                disagreement_found = True
                #if we get to the end, and disagreements were found, that means the entire database is in disagreement. 
                #Will throw an error below.
            
        if disagreement_found:
            raise DatabaseResyncRequired()
        else:
            return None, None
            
        
    
    #TODO. make sure we remove conflict blocks that reach consensus   
    async def get_block_conflict_consensus(self, chain_wallet_address, block_number):
        '''
        Returns the block hash of the block that has consensus for a given block_conflict.
        '''
        #first lets double check which block hash we have:
        #TODO: might want to streamlinethis by storing it in the local variable
        try:
            local_block_hash = await self.chaindb.coro_get_canonical_block_hash(block_number, chain_wallet_address)
        except HeaderNotFound:
            local_block_hash = None
        
#        self.block_choice_consensus = {}
#        for chain_wallet_address, block_numbers in self.block_choice_statistics.items():
#            block_number_consensus = {}
#            for block_number, block_hash_stakes in block_numbers.items():
#                block_number_consensus[block_number] = self.determine_stake_winner(block_hash_stakes)
#            self.block_choice_consensus[chain_wallet_address] = block_number_consensus
#            
            
        
        try:
            block_hash_stakes = self.block_choice_statistics[chain_wallet_address][block_number]
            peer_consensus_block_hash, peer_consensus_block_stake = self.determine_stake_winner(block_hash_stakes)
            #peer_consensus_block_hash, peer_consensus_block_stake = self.block_choice_consensus[chain_wallet_address][block_number]
        except KeyError:
            peer_consensus_block_hash = None
        
        if peer_consensus_block_hash is None:
            return local_block_hash
        
        else:
            if local_block_hash is None:
                return peer_consensus_block_hash
            else:
                if local_block_hash != peer_consensus_block_hash:
                    #the peers have chosen something different than what we have here
                    #At this point we calculate the stake of all children blocks that come after it
                    #However, we don't want to count any nodes that have voted here incase their vote changed
                    exclude_chains = list(self.peer_block_choices.keys())
                    children_stake_for_local_block = await self.chain.coro_get_block_stake_from_children(local_block_hash, exclude_chains = exclude_chains)
                    our_stake_for_local_block = await self.chain.coro_get_mature_stake(self.chain_config.node_wallet_address)
                    
                    try:
                        peer_stake_for_local_block = self.block_choice_statistics[chain_wallet_address][block_number][local_block_hash]
                    except KeyError:
                        peer_stake_for_local_block = 0
                    total_stake_for_local_block =  peer_stake_for_local_block + children_stake_for_local_block + our_stake_for_local_block
                    
                    return self.get_winner_stake_binary_compare(peer_consensus_block_hash, 
                                                                peer_consensus_block_stake, 
                                                                local_block_hash, 
                                                                total_stake_for_local_block)
                



    '''
    message handling stuff
    '''
    async def _handle_msg(self, peer: HLSPeer, cmd: protocol.Command,
                          msg: protocol._DecodedMsgType) -> None:

        if isinstance(cmd, hls.UnorderedBlockHeaderHash):
            await self._handle_block_choices(peer, cast(List[hls.BlockHashKey], msg))

        if isinstance(cmd, hls.GetUnorderedBlockHeaderHash):
            await self._handle_get_block_choices(peer, cast(List[hls.BlockNumberKey], msg))
            
        if isinstance(cmd, hls.ChainHeadRootHashTimestamps):
            await self._handle_chain_head_root_hash_timestamps(peer, cast(List[Any], msg))
            
        if isinstance(cmd, hls.GetChainHeadRootHashTimestamps):
            await self._handle_get_chain_head_root_hash_timestamps(peer, cast(Dict[str, Any], msg))
        
        if isinstance(cmd, hls.StakeForAddresses):
            await self._handle_stake_for_addresses(peer, cast(Dict[str, Any], msg))
            
        if isinstance(cmd, hls.GetStakeForAddresses):
            await self._handle_get_stake_for_addresses(peer, cast(Dict[str, Any], msg))
            
        if isinstance(cmd, hls.GetMinGasParameters):
            await self._handle_get_min_gas_parameters(peer, cast(Dict[str, Any], msg))
            
        if isinstance(cmd, hls.MinGasParameters):
            await self._handle_min_gas_parameters(peer, cast(Dict[str, Any], msg))
        
        


    async def _handle_block_choices(self, peer: HLSPeer, msg) -> None:
        peer_wallet_address = peer.wallet_address
        #self.logger.debug("handle_block_chioces msg = {}".format(msg))
        stake = await self.get_accurate_stake(peer_wallet_address, peer.stake)
        new_peer_block_choice = [peer_wallet_address, stake, msg]
        self._new_peer_block_choices.put_nowait(new_peer_block_choice)
            
    async def _handle_get_block_choices(self, peer: HLSPeer, msg) -> None:
        #self.logger.debug("_handle_get_block_choices msg = {}".format(msg))        
        #lets get the data and send it back
        block_keys = msg
        return_data = []
        for block_key in block_keys:
            block_hash = await self.chain_db.coro_get_canonical_block_hash(block_key.block_number, block_key.wallet_address)
            return_data.append(BlockHashKey(wallet_address = block_key.wallet_address, block_number = block_key.block_number, block_hash = block_hash))
        
        if len(return_data) > 0:
            peer.sub_proto.send_unordered_block_header_hash(return_data)
      
        
    async def _handle_chain_head_root_hash_timestamps(self, peer: HLSPeer, msg) -> None:
        peer_wallet_address = peer.wallet_address
        #self.logger.debug("_handle_chain_head_root_hash_timestamps msg = {}".format(msg))
        
        stake = await self.get_accurate_stake(peer_wallet_address, peer.stake)
        new_root_hash_timestamps = msg
        #lets save it to the peer
        peer.chain_head_root_hashes = new_root_hash_timestamps
        new_peer_data = [peer_wallet_address, stake, new_root_hash_timestamps]
        self._new_peer_chain_head_root_hash_timestamps.put_nowait(new_peer_data)
            
    async def _handle_get_chain_head_root_hash_timestamps(self, peer: HLSPeer, msg) -> None:
        #peer_wallet_address = peer.wallet_address
        #self.logger.debug("_handle_get_chain_head_root_hash_timestamps msg = {}".format(msg))       
        #lets get the data and send it back
        return_data = await self.chain_head_db.coro_get_historical_root_hashes(msg['after_timestamp'])
        if return_data is not None:
            #self.logger.debug("_handle_get_chain_head_root_hash_timestamps return_data = {}".format(return_data))
            peer.sub_proto.send_chain_head_root_hash_timestamps(return_data)
            
    async def _handle_stake_for_addresses(self, peer: HLSPeer, msg) -> None:
        #make sure it is a bootstrap node
        if peer.remote in self.bootstrap_nodes:
            for address_stake in msg['stakes']:
                address = address_stake[0]
                stake = address_stake[1]
                self.peer_stake_from_bootstrap_node[address] = stake
            
    async def _handle_get_stake_for_addresses(self, peer: HLSPeer, msg) -> None:
        address_stakes = []
        for address in msg['addresses']:
            stake = await self.chain.coro_get_mature_stake(address)
            address_stakes.append([address,stake])
        peer.sub_proto.send_stake_for_addresses(address_stakes)
        
    async def _handle_get_min_gas_parameters(self, peer: HLSPeer, msg) -> None:
        hist_min_allowed_gas_price = await self.chaindb.coro_load_historical_minimum_gas_price(make_mutable = False, sort=True)
        
        if msg['num_centiseconds_from_now'] == 0:
            average_network_tpc_cap = await self.calculate_average_network_tpc_cap()
            if average_network_tpc_cap is not None:
                hist_net_tpc_capability = [[0, average_network_tpc_cap]]
                hist_min_allowed_gas_price_new = [[0,hist_min_allowed_gas_price[-1][1]]]
                peer.sub_proto.send_min_gas_initialization(hist_net_tpc_capability, hist_min_allowed_gas_price_new)
        else:
            hist_net_tpc_capability = await self.chaindb.coro_load_historical_network_tpc_capability(make_mutable = False, sort=True)
            
            num_centiseconds_to_send = min([len(hist_net_tpc_capability), len(hist_min_allowed_gas_price), msg['num_centiseconds_from_now']])
        
            peer.sub_proto.send_min_gas_initialization(hist_net_tpc_capability[-num_centiseconds_to_send:], hist_min_allowed_gas_price[-num_centiseconds_to_send:])
            
    async def _handle_min_gas_parameters(self, peer: HLSPeer, msg) -> None:
        
        hist_net_tpc_capability = msg['hist_net_tpc_capability']
        hist_min_allowed_gas_price = msg['hist_min_allowed_gas_price']
        
        if len(hist_net_tpc_capability) == 1:
            #require that the timestamp is set to 0 for the most recent average. Otherwise we shouldn't be receiving just 1.
            if hist_net_tpc_capability[0][0] == 0:
                #We are just receiving the last minute. this can be from any node. Lets keep track of it and average it.
                peer_wallet_address = peer.wallet_address
                stake = await self.get_accurate_stake(peer_wallet_address, peer.stake)
                self._network_tpc_cap_statistics[peer_wallet_address] = [time.time(),hist_net_tpc_capability[0][1], stake]
            
        #make sure they are a bootnode
        if peer.remote in self.bootstrap_nodes:
            if await self.chaindb.coro_min_gas_system_initialization_required():
                self.chaindb.coro_save_historical_minimum_gas_price(hist_min_allowed_gas_price)
                self.chaindb.coro_save_historical_network_tpc_capability(hist_net_tpc_capability)
                
            
            
            
            
            
            
            
            
            
            
            
            
            
            
            