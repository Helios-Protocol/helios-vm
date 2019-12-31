from abc import (
    ABCMeta,
    abstractmethod
)
from uuid import UUID
import traceback
import logging
from lru import LRU
from typing import Set, Tuple, List, Optional, Union  # noqa: F401

from eth_typing import Hash32

import rlp_cython as rlp

from trie import (
    HexaryTrie,
)

from hvm.exceptions import ReceivableTransactionNotFound,StateRootNotFound, ValidationError
from eth_hash.auto import keccak
from eth_utils import encode_hex

from hvm.constants import (
    BLANK_ROOT_HASH,
    EMPTY_SHA3,
    SLASH_WALLET_ADDRESS,
)
from hvm.db.batch import (
    BatchDB,
)
from hvm.db.cache import (
    CacheDB,
)
from hvm.db.journal import (
    JournalDB,
)
from hvm.rlp.accounts import (
    Account,
    TransactionKey,
    AccountDepreciated)
from hvm.validation import (
    validate_is_bytes,
    validate_uint256,
    validate_canonical_address,
    validate_word)

from hvm.utils.numeric import (
    int_to_big_endian,
)
from hvm.utils.padding import (
    pad32,
)

from hvm.db.schema import SchemaV1

from .hash_trie import HashTrie

from eth_typing import Address, Hash32
from hvm.rlp.sedes import(
    address,

)



# Use lru-dict instead of functools.lru_cache because the latter doesn't let us invalidate a single
# entry, so we'd have to invalidate the whole cache in _set_account() and that turns out to be too
# expensive.
account_cache = LRU(2048)


class BaseAccountDB(metaclass=ABCMeta):

    @abstractmethod
    def __init__(self) -> None:
        raise NotImplementedError(
            "Must be implemented by subclasses"
        )

    #
    # Storage
    #
    @abstractmethod
    def get_storage(self, address, slot):
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def set_storage(self, address, slot, value):
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Balance
    #
    @abstractmethod
    def get_balance(self, address):
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def set_balance(self, address, balance):
        raise NotImplementedError("Must be implemented by subclasses")

    def delta_balance(self, address, delta):
        if delta != 0:
            new_balance = self.get_balance(address) + delta
            if new_balance < 0:
                raise ValidationError("Cannod delta balance because account does not have anough funds. Account balance: {} | delta: {}".format(self.get_balance(address), delta))
            self.set_balance(address, self.get_balance(address) + delta)

    #
    # Receivable Transactions
    #
    @abstractmethod
    def get_receivable_transactions(self, address: Address) -> List[TransactionKey]:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def has_receivable_transactions(self, address: Address) -> bool:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def get_receivable_transaction(self, address: Address, transaction_hash: Hash32) -> Optional[TransactionKey]:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def add_receivable_transactions(self, address: Address, transaction_keys: TransactionKey) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def add_receivable_transaction(self,
                                   address: Address,
                                   transaction_hash: Hash32,
                                   sender_block_hash: Hash32,
                                   is_contract_deploy: bool = False,
                                   refund_amount=0) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def delete_receivable_transaction(self, address: Address, transaction_hash: Hash32) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def save_receivable_transaction_as_not_imported(self, address: Address, transaction_hash: Hash32) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def get_receivable_transaction_saved_as_not_imported(self, address: Address, transaction_hash: Hash32) -> TransactionKey:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def delete_receivable_transaction_saved_as_not_imported(self, address: Address, transaction_hash: Hash32) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Nonce
    #

    @abstractmethod
    def get_nonce(self, address: Address) -> int:
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Gas refunds
    #
    @abstractmethod
    def save_refund_amount_for_transaction(self, tx_hash: Hash32, refund_amount: int) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def get_refund_amount_for_transaction(self, tx_hash: Hash32) -> int:
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Code
    #
    @abstractmethod
    def set_code(self, address, code):
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def get_code(self, address):
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def get_code_hash(self, address):
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def delete_code(self, address):
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Internal use smart contract transaction queue system
    #
    @abstractmethod
    def _add_address_to_smart_contracts_with_pending_transactions(self, address: Address) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def _remove_address_from_smart_contracts_with_pending_transactions(self, address: Address) -> None:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def get_smart_contracts_with_pending_transactions(self) -> List[Address]:
        raise NotImplementedError("Must be implemented by subclasses")

    @abstractmethod
    def is_smart_contract(self, address: Address) -> bool:
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Account Methods
    #
    @abstractmethod
    def account_is_empty(self, address):
        raise NotImplementedError("Must be implemented by subclass")

    @abstractmethod
    def account_has_chain(self, address: Address) -> bool:
        raise NotImplementedError("Must be implemented by subclass")

    @abstractmethod
    def get_account_hash(self, address: Address) -> Hash32:
        raise NotImplementedError("Must be implemented by subclass")

    #
    # Record and discard API
    #
    @abstractmethod
    def record(self) -> Tuple[UUID, UUID]:
        raise NotImplementedError("Must be implemented by subclass")

    @abstractmethod
    def discard(self, changeset: Tuple[UUID, UUID]) -> None:
        raise NotImplementedError("Must be implemented by subclass")

    @abstractmethod
    def commit(self, changeset: Tuple[UUID, UUID]) -> None:
        raise NotImplementedError("Must be implemented by subclass")


    @abstractmethod
    def persist(self) -> None:
        """
        Send changes to underlying database, including the trie state
        so that it will forever be possible to read the trie from this checkpoint.
        """
        raise NotImplementedError("Must be implemented by subclass")


class AccountDB(BaseAccountDB):
    version = 0
    logger = logging.getLogger('hvm.db.account.AccountDB')

    def __init__(self, db):
        r"""
        Internal implementation details (subject to rapid change):

        Journaling sequesters writes at the _journal* attrs ^, until persist is called.

        _batchdb and _batchtrie together enable us to make the state root,
        without saving everything to the database.

        _journaldb is a journaling of the keys and values used to store
        code and account storage.

        TODO: add cache
        _trie_cache is a cache tied to the state root of the trie. It
        is important that this cache is checked *after* looking for
        the key in _journaltrie, because the cache is only invalidated
        after a state root change.

        AccountDB synchronizes the snapshot/revert/persist the
        journal.
        """
        self.db = db
        self._batchdb = BatchDB(db)
        self._journaldb = JournalDB(self._batchdb)





    #
    # Storage
    #
    def get_storage(self, address, slot, from_journal = True):
        
        validate_canonical_address(address, title="Storage Address")
        validate_uint256(slot, title="Storage Slot")

        if from_journal:
            account = self._get_account(address)
            storage = HashTrie(HexaryTrie(self._journaldb, account.storage_root))
        else:
            orig_journal_db = self._journaldb
            self._journaldb = self.db
            account = self._get_account(address, save_upgraded_account=False)
            storage = HashTrie(HexaryTrie(self._journaldb, account.storage_root))
            self._journaldb = orig_journal_db


        slot_as_key = pad32(int_to_big_endian(slot))

        if slot_as_key in storage:
            encoded_value = storage[slot_as_key]
            to_return = rlp.decode(encoded_value, sedes=rlp.sedes.big_endian_int)
        else:
            to_return = 0
        
        self.logger.debug("getting storage for address {} | slot {} | value {}".format(encode_hex(address), slot, to_return))

        return to_return

    def set_storage(self, address, slot, value):
        self.logger.debug("Setting storage for address {} | slot {} | value {}".format(encode_hex(address), slot, value))
        validate_uint256(value, title="Storage Value")
        validate_uint256(slot, title="Storage Slot")
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        storage = HashTrie(HexaryTrie(self._journaldb, account.storage_root))

        slot_as_key = pad32(int_to_big_endian(slot))

        if value:
            encoded_value = rlp.encode(value)
            storage[slot_as_key] = encoded_value
        else:
            del storage[slot_as_key]

        self._set_account(address, account.copy(storage_root=storage.root_hash))

    def delete_storage(self, address):
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        self._set_account(address, account.copy(storage_root=BLANK_ROOT_HASH))

    #
    # Balance
    #
    def get_balance(self, address):
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        return account.balance

    def set_balance(self, address, balance):
        validate_canonical_address(address, title="Storage Address")
        validate_uint256(balance, title="Account Balance")

        account = self._get_account(address)
        self._set_account(address, account.copy(balance=balance))

    #
    # Nonce
    #
    def get_nonce(self, address: Address) -> int:
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        return account.nonce

    def set_nonce(self, address, nonce):
        validate_canonical_address(address, title="Storage Address")
        validate_uint256(nonce, title="Nonce")

        account = self._get_account(address)
        self._set_account(address, account.copy(nonce=nonce))

    def increment_nonce(self, address):
        current_nonce = self.get_nonce(address)
        self.set_nonce(address, current_nonce + 1)

    #
    # Block number
    #
    def get_block_number(self, address):
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        return account.block_number

    def set_block_number(self, address, block_number):
        validate_canonical_address(address, title="Storage Address")
        validate_uint256(block_number, title="Block Number")

        account = self._get_account(address)
        self._set_account(address, account.copy(block_number=block_number))

    def increment_block_number(self, address):
        current_block_number = self.get_block_number(address)
        self.set_block_number(address, current_block_number + 1)
        
       
    #
    # Receivable Transactions
    #
    def get_receivable_transactions(self, address: Address) -> List[TransactionKey]:
        validate_canonical_address(address, title="Storage Address")
        receivable_transactions_lookup_key = SchemaV1.make_account_receivable_transactions_lookup_key(address)
        try:
            encoded = self._journaldb[receivable_transactions_lookup_key]
            return rlp.decode(encoded, sedes=rlp.sedes.CountableList(TransactionKey), use_list = True)
        except KeyError:
            return []

    def save_receivable_transactions(self, address: Address, transaction_keys: List[TransactionKey]) -> None:
        receivable_transactions_lookup_key = SchemaV1.make_account_receivable_transactions_lookup_key(address)
        encoded = rlp.encode(transaction_keys, sedes=rlp.sedes.CountableList(TransactionKey))
        self._journaldb[receivable_transactions_lookup_key] = encoded

    def save_receivable_transactions_if_none_exist(self, address: Address, transaction_keys: List[TransactionKey]) -> None:
        existing_txs = self.get_receivable_transactions(address)
        if len(existing_txs) == 0:
            self.save_receivable_transactions(address, transaction_keys)



    def has_receivable_transactions(self, address: Address) -> bool:
        tx = self.get_receivable_transactions(address)
        if len(tx) == 0:
            return False
        else:
            return True
        
    def get_receivable_transaction(self, address: Address, transaction_hash: Hash32) -> Optional[TransactionKey]:
        validate_is_bytes(transaction_hash, title="Transaction Hash")
        all_tx = self.get_receivable_transactions(address)
        for tx_key in all_tx:
            if tx_key.transaction_hash == transaction_hash:
                return tx_key
        return None
        
     
    def add_receivable_transactions(self, address: Address, transaction_keys: List[TransactionKey]) -> None:
        validate_canonical_address(address, title="Wallet Address")
        for tx_key in transaction_keys:
            self.add_receivable_transaction(address, tx_key.transaction_hash, tx_key.sender_block_hash)




    def add_receivable_transaction(self,
                                   address: Address,
                                   transaction_hash: Hash32,
                                   sender_block_hash: Hash32,
                                   is_contract_deploy:bool = False,
                                   refund_amount = 0) -> None:
        self.logger.debug("Adding receivable transaction {} to address {}".format(encode_hex(transaction_hash), encode_hex(address)))
        validate_canonical_address(address, title="Wallet Address")
        validate_is_bytes(transaction_hash, title="Transaction Hash")
        validate_is_bytes(sender_block_hash, title="Sender Block Hash")
        
        receivable_transactions = self.get_receivable_transactions(address)

        # first lets make sure we don't already have the transaction
        for tx_key in receivable_transactions:
            if tx_key.transaction_hash == transaction_hash:
                raise ValueError("Tried to save a receivable transaction that was already saved. TX HASH = {}".format(encode_hex(transaction_hash)))


        receivable_transactions.append(TransactionKey(transaction_hash, sender_block_hash))
        self.save_receivable_transactions(address, receivable_transactions)

        self.save_refund_amount_for_transaction(transaction_hash, refund_amount)

        #finally, if this is a smart contract, lets add it to the list of smart contracts with pending transactions
        if is_contract_deploy or self.get_code_hash(address) != EMPTY_SHA3:
            self.logger.debug("Adding address to list of smart contracts with pending transactions")
            #we only need to run this when adding the first one.
            self._add_address_to_smart_contracts_with_pending_transactions(address)
        
    def delete_receivable_transaction(self, address: Address, transaction_hash: Hash32) -> None:
        validate_canonical_address(address, title="Storage Address")
        validate_is_bytes(transaction_hash, title="Transaction Hash")
        
        self.logger.debug("deleting receivable tx {} from account {}".format(encode_hex(transaction_hash), encode_hex(address)))

        receivable_transactions = self.get_receivable_transactions(address)

        i = 0
        found = False
        for tx_key in receivable_transactions:
            if tx_key.transaction_hash == transaction_hash:
                found = True
                break
            i +=1
            
        if found == True:
            del receivable_transactions[i]
        else:
            raise ReceivableTransactionNotFound("transaction hash {0} not found in receivable_transactions database for wallet {1}".format(transaction_hash, address))
        
        self.save_receivable_transactions(address, receivable_transactions)

        if self.get_code_hash(address) != EMPTY_SHA3:
            if len(receivable_transactions) == 0:
                self.logger.debug("Removing address from list of smart contracts with pending transactions")
                self._remove_address_from_smart_contracts_with_pending_transactions(address)

    def save_receivable_transaction_as_not_imported(self, address: Address, transaction_hash: Hash32) -> None:
        # moves a receivable transaction into the not imported state. it will no longer be loaded with the other receivable transactions
        tx_key = self.get_receivable_transaction(address, transaction_hash)
        if tx_key is not None:
            lookup_key = SchemaV1.make_save_receivable_transaction_as_not_imported_lookup(address, transaction_hash)
            encoded = rlp.encode(tx_key, sedes=TransactionKey)
            self._journaldb[lookup_key] = encoded
            self.delete_receivable_transaction(address, transaction_hash)

    def get_receivable_transaction_saved_as_not_imported(self, address: Address, transaction_hash: Hash32) -> TransactionKey:
        lookup_key = SchemaV1.make_save_receivable_transaction_as_not_imported_lookup(address, transaction_hash)
        try:
            encoded = self._journaldb[lookup_key]
            return rlp.decode(encoded, sedes=TransactionKey)
        except KeyError:
            return None

    def delete_receivable_transaction_saved_as_not_imported(self, address: Address, transaction_hash: Hash32) -> None:
        lookup_key = SchemaV1.make_save_receivable_transaction_as_not_imported_lookup(address, transaction_hash)
        try:
            del self._journaldb[lookup_key]
        except KeyError:
            pass


    #
    # Gas refunds
    #
    def save_refund_amount_for_transaction(self, tx_hash: Hash32, refund_amount: int) -> None:
        if refund_amount > 0:
            self.logger.debug("SAVING REFUND AMOUNT {} FOR TX HASH {}".format(refund_amount, encode_hex(tx_hash)))
            validate_word(tx_hash, title="tx_hash")
            validate_uint256(refund_amount, title="refund_amount")
            lookup_key = SchemaV1.make_transaction_refund_amount_lookup(tx_hash)

            encoded = rlp.encode(refund_amount, sedes=rlp.sedes.big_endian_int)
            self._journaldb[lookup_key] = encoded

    def get_refund_amount_for_transaction(self, tx_hash: Hash32) -> int:
        self.logger.debug("GETTING REFUND AMOUNT FOR TX HASH {}".format(encode_hex(tx_hash)))
        lookup_key = SchemaV1.make_transaction_refund_amount_lookup(tx_hash)
        try:
            encoded = self._journaldb[lookup_key]
            decoded = rlp.decode(encoded, sedes=rlp.sedes.big_endian_int)
            return decoded
        except KeyError:
            return 0


    #
    # Code
    #
    def get_code(self, address):
        validate_canonical_address(address, title="Storage Address")

        try:
            return self._journaldb[self.get_code_hash(address)]
        except KeyError:
            return b""

    def set_code(self, address, code):
        validate_canonical_address(address, title="Storage Address")
        validate_is_bytes(code, title="Code")

        account = self._get_account(address)

        code_hash = keccak(code)
        self._journaldb[code_hash] = code
        self._set_account(address, account.copy(code_hash=code_hash))

    def get_code_hash(self, address):
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        return account.code_hash

    def delete_code(self, address):
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        self._set_account(address, account.copy(code_hash=EMPTY_SHA3))


    #
    # Internal use smart contract transaction queue system
    #
    def _add_address_to_smart_contracts_with_pending_transactions(self, address: Address) -> None:
        key = SchemaV1.make_smart_contracts_with_pending_transactions_lookup_key()

        address_set = set(self.get_smart_contracts_with_pending_transactions())

        address_set.add(address)

        self._journaldb[key] = rlp.encode(list(address_set), sedes=rlp.sedes.FCountableList(address))

    def _remove_address_from_smart_contracts_with_pending_transactions(self, address: Address) -> None:
        key = SchemaV1.make_smart_contracts_with_pending_transactions_lookup_key()

        address_set = set(self.get_smart_contracts_with_pending_transactions())

        address_set.remove(address)

        self._journaldb[key] = rlp.encode(list(address_set), sedes=rlp.sedes.FCountableList(address))

    def has_pending_smart_contract_transactions(self, address: Address) -> bool:
        validate_canonical_address(address, title="Storage Address")
        address_set = set(self.get_smart_contracts_with_pending_transactions())
        return address in address_set

    def get_smart_contracts_with_pending_transactions(self) -> List[Address]:
        key = SchemaV1.make_smart_contracts_with_pending_transactions_lookup_key()

        try:
            address_list = rlp.decode(self._journaldb[key], sedes=rlp.sedes.FCountableList(address), use_list=True)
            return address_list
        except KeyError:
            return []

    def is_smart_contract(self, address: Address) -> bool:
        return self.account_has_code(address) or self.has_pending_smart_contract_transactions(address)

    #
    # Account Methods
    #
    def account_has_code_or_nonce(self, address):
        return self.get_nonce(address) != 0 or self.account_has_code(address)

    def account_has_code(self, address: Address) -> bool:

        return self.get_code_hash(address) != EMPTY_SHA3

    def delete_account(self, address):
        validate_canonical_address(address, title="Storage Address")
        account_lookup_key = SchemaV1.make_account_lookup_key(address)
        #try:
        del self._journaldb[account_lookup_key]
        #except KeyError:
        #    self.logger.debug("tried to delete an account that doesnt exist")

    def account_exists(self, address):
        validate_canonical_address(address, title="Storage Address")
        account_lookup_key = SchemaV1.make_account_lookup_key(address)

        try:
            rlp_account = self._journaldb[account_lookup_key]
            return True
        except KeyError:
            return False


    def touch_account(self, address: Address):
        validate_canonical_address(address, title="Storage Address")

        account = self._get_account(address)
        self._set_account(address, account)

    def account_is_empty(self, address: Address) -> bool:
        return not self.account_has_code_or_nonce(address) and self.get_balance(address) == 0 and self.has_receivable_transactions(address) is False
    
    def account_has_chain(self, address: Address) -> bool:
        if not self.account_exists(address):
            return False
        elif not self.account_has_code_or_nonce(address) and self.get_balance(address) == 0:
            return False
        else:
            return True

        


    def get_account_hash(self, address: Address) -> Hash32:
        account = self._get_account(address)
        #
        # Backwards compatability so that the hashes are still correct for old blocks. This is fixed in next fork.
        #
        hashable_account = AccountDepreciated(
            account.nonce,
            account.block_number,
            (),
            (),
            account.balance,
            account.storage_root,
            account.code_hash
        )
        account_hashable_encoded = rlp.encode(hashable_account, sedes=AccountDepreciated)
        return keccak(account_hashable_encoded)
    
    #
    # Internal
    #

    def _get_account_version(self, address_or_hash: Union[Address, Hash32]) -> int:
        account_version_lookup_key = SchemaV1.make_account_version_lookup_key(address_or_hash)
        try:
            account_version_encoded = self._journaldb[account_version_lookup_key]
            return rlp.decode(account_version_encoded, sedes=rlp.sedes.f_big_endian_int)
        except KeyError:
            return -1


    def _set_account_version(self, address_or_hash: Union[Address, Hash32], version: int) -> None:
        self.logger.debug('Saving account or hash {} as version {}'.format(encode_hex(address_or_hash), self.version))
        account_version_lookup_key = SchemaV1.make_account_version_lookup_key(address_or_hash)
        encoded = rlp.encode(version, sedes=rlp.sedes.f_big_endian_int)
        self._journaldb[account_version_lookup_key] = encoded

    def _decode_and_upgrade_account(self, rlp_account: bytes, address: Address, account_version: int, save_new_account = True) -> Account:
        if rlp_account:
            if account_version == self.version:
                account = rlp.decode(rlp_account, sedes=Account)
            elif account_version == -1:
                self.logger.debug("Found a depreciated account that needs upgrading.")
                #we need to upgrade this from the depreciated version
                depreciated_account = rlp.decode(rlp_account, sedes=AccountDepreciated)
                account = Account(
                    depreciated_account.nonce,
                    depreciated_account.block_number,
                    depreciated_account.balance,
                    depreciated_account.storage_root,
                    depreciated_account.code_hash
                )
                if save_new_account:
                    # remember to also save receivable transactions
                    receivable_transactions = depreciated_account.receivable_transactions
                    self.save_receivable_transactions_if_none_exist(address, receivable_transactions)

                    # Lets immediately set the new account
                    self._set_account(address = address, account = account)
            else:
                raise ValidationError("The loaded account is from an unknown account version {}. This account db is version {}".format(account_version, self.version))

        else:
            account = Account()
        return account


    def _get_account(self, address: Address, save_upgraded_account = True) -> Account:
        account_lookup_key = SchemaV1.make_account_lookup_key(address)
        try:
            rlp_account = self._journaldb[account_lookup_key]
        except KeyError:
            rlp_account = b''
        account_version = self._get_account_version(address)
        account = self._decode_and_upgrade_account(rlp_account, address, account_version, save_upgraded_account)

        return account


    def _set_account(self, address: Address, account: Account) -> None:
        encoded_account = rlp.encode(account, sedes=Account)
        #encoded_account = hm_encode(account)
        account_lookup_key = SchemaV1.make_account_lookup_key(address)
        self._journaldb[account_lookup_key] = encoded_account

        # set the account version
        self._set_account_version(address, self.version)

    #
    # Record and discard API
    #
    def record(self) -> UUID:
        self.logger.debug("Recording account db changeset")
        return (self._journaldb.record())

    def discard(self, changeset: UUID) -> None:
        self.logger.debug("Discarding account db changes")
        db_changeset = changeset
        self._journaldb.discard(db_changeset)

    def commit(self, changeset: UUID) -> None:
        db_changeset = changeset
        self._journaldb.commit(db_changeset)

    def persist(self, save_account_hash = False, wallet_address = None) -> None:
        self.logger.debug('Persisting account db. save_account_hash {} | wallet_address {}'.format(save_account_hash, wallet_address))
        # Check to see if it needs to be upgraded, and do it now
        self._journaldb.persist()
        self._batchdb.commit(apply_deletes=True)
        
        if save_account_hash:
            validate_canonical_address(wallet_address, title="Address")
            self.save_current_account_with_hash_lookup(wallet_address)
      
    #
    # Saving account state at particular account hash
    #
    
    def save_current_account_with_hash_lookup(self, wallet_address):
        validate_canonical_address(wallet_address, title="Address")
        account_hash = self.get_account_hash(wallet_address)
        account = self._get_account(wallet_address)
        rlp_account = rlp.encode(account, sedes=Account)
        
        lookup_key = SchemaV1.make_account_by_hash_lookup_key(account_hash)
        self.db[lookup_key] = rlp_account

        # set the account version
        self._set_account_version(account_hash, self.version)

        
    
    def revert_to_account_from_hash(self, account_hash, wallet_address):
        validate_canonical_address(wallet_address, title="Address")
        validate_is_bytes(account_hash, title="account_hash")
        lookup_key = SchemaV1.make_account_by_hash_lookup_key(account_hash)
        try:
            rlp_encoded = self.db[lookup_key]
            account_version = self._get_account_version(account_hash)
            account = self._decode_and_upgrade_account(rlp_encoded, wallet_address, account_version)
            self._set_account(wallet_address, account)
        except KeyError:
            raise StateRootNotFound()
            
        
        

