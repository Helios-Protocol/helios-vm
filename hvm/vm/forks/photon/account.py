from hvm.db.account import AccountDB
from hvm.db.schema import SchemaV1
import rlp_cython as rlp

from hvm.rlp.accounts import (
    Account,
    TransactionKey,
    AccountDepreciated)
from hvm.types import Timestamp

from rlp_cython.sedes import (
    big_endian_int,
    CountableList,
    FCountableList,
    f_big_endian_int
)

from hvm.constants import (
    EMPTY_SHA3,
    BLANK_ROOT_HASH,
    ZERO_ADDRESS,
)

from eth_hash.auto import keccak

from hvm.rlp.sedes import (
    trie_root,
    hash32,
    address

)

from hvm.exceptions import StateRootNotFound, ValidationError

from eth_typing import Address

from typing import Any

from hvm.validation import (
    validate_is_bytes,
    validate_uint256,
    validate_canonical_address,
)
from eth_typing import Hash32

from eth_utils import int_to_big_endian, encode_hex

from hvm.db.hash_trie import HashTrie

from trie import HexaryTrie

from hvm.utils.padding import pad32

class PhotonAccount(rlp.Serializable):
    """
    RLP object for accounts.
    """
    fields = [
        ('nonce', f_big_endian_int),
        ('contract_deploy_timestamp', f_big_endian_int),
        ('balance', big_endian_int),
        ('storage_root', trie_root),
        ('external_smart_contract_storage_root', trie_root),
        ('code_hash', hash32)
    ]

    # external_smart_contract_storage_root is a trie that acts as a db mapping "smart contract address" -> "storage root"
    # The storage root that it points to is the storage that only code located at "smart contract address" can manipulate
    # It is not the global storage for that smart contract, it is storage specific to this chain.

    def __init__(self,
                 nonce: int=0,
                 contract_deploy_timestamp: int=0,
                 balance: int=0,
                 storage_root: bytes=BLANK_ROOT_HASH,
                 external_smart_contract_storage_root: bytes = BLANK_ROOT_HASH,
                 code_hash: bytes=EMPTY_SHA3,
                 **kwargs: Any) -> None:
        super(PhotonAccount, self).__init__(nonce, contract_deploy_timestamp, balance, storage_root, external_smart_contract_storage_root, code_hash, **kwargs)


class PhotonAccountDB(AccountDB):

    version = 1

    #
    # Block number
    #
    def get_block_number(self, address):
        raise NotImplementedError('Depreciated')

    def set_block_number(self, address, block_number):
        raise NotImplementedError('Depreciated')

    def increment_block_number(self, address):
        raise NotImplementedError('Depreciated')


    #
    # Contract deploy timestamp
    #
    def get_contract_deploy_timestamp(self, address: Address) -> Timestamp:
        validate_canonical_address(address, title="address")
        account = self._get_account(address)
        return account.contract_deploy_timestamp

    def set_contract_deploy_timestamp(self, address: Address, timestamp: Timestamp) -> None:
        validate_canonical_address(address, title="address")
        validate_uint256(timestamp, title="timestamp")

        account = self._get_account(address)
        self._set_account(address, account.copy(contract_deploy_timestamp=timestamp))



    #
    # Storage
    #

    def get_external_smart_contract_storage(self, address: Address, smart_contract_address: Address, slot: int, from_journal = True) -> bytes:
        if address == smart_contract_address:
            # External smart contract storage for the same chain is just normal storage
            self.logger.debug("Redirecting get_external_smart_contract_storage to get_storage for address {}".format(encode_hex(address)))
            return self.get_storage(address, slot, from_journal)

        validate_canonical_address(address, title="Storage Address")
        validate_canonical_address(smart_contract_address, title="smart_contract_address")
        validate_uint256(slot, title="Storage Slot")

        if from_journal:
            account = self._get_account(address)
            external_smart_contract_storage_roots = HexaryTrie(self._journaldb, account.external_smart_contract_storage_root)
        else:
            orig_journal_db = self._journaldb
            self._journaldb = self.db
            account = self._get_account(address, save_upgraded_account=False)
            external_smart_contract_storage_roots = HexaryTrie(self._journaldb, account.external_smart_contract_storage_root)




        if smart_contract_address in external_smart_contract_storage_roots:
            external_smart_contract_storage_root = external_smart_contract_storage_roots[smart_contract_address]

            storage = HashTrie(HexaryTrie(self._journaldb, external_smart_contract_storage_root))

            slot_as_key = pad32(int_to_big_endian(slot))

            if slot_as_key in storage:
                encoded_value = storage[slot_as_key]
                to_return = rlp.decode(encoded_value, sedes=rlp.sedes.big_endian_int)
            else:
                to_return = 0
        else:
            to_return = 0

        self.logger.debug("getting smart contract storage for address {} | smart_contract_address {} | slot {} | value {}".format(encode_hex(address), encode_hex(smart_contract_address), slot, to_return))
        if not from_journal:
            self._journaldb = orig_journal_db

        return to_return


    def set_external_smart_contract_storage(self, address: Address, smart_contract_address: Address, slot: int, value: int) -> None:
        if address == smart_contract_address:
            # External smart contract storage for the same chain is just normal storage
            self.logger.debug("Redirecting set_external_smart_contract_storage to set_storage for address {}".format(encode_hex(address)))
            return self.set_storage(address, slot, value)

        validate_uint256(value, title="Storage Value")
        validate_uint256(slot, title="Storage Slot")
        validate_canonical_address(address, title="Storage Address")
        validate_canonical_address(smart_contract_address, title="smart_contract_address")

        account = self._get_account(address)

        external_smart_contract_storage_roots = HexaryTrie(self._journaldb, account.external_smart_contract_storage_root)

        try:
            external_smart_contract_storage_root = external_smart_contract_storage_roots[smart_contract_address]
            storage = HashTrie(HexaryTrie(self._journaldb, external_smart_contract_storage_root))
        except KeyError:
            storage = HashTrie(HexaryTrie(self._journaldb))

        slot_as_key = pad32(int_to_big_endian(slot))

        if value:
            encoded_value = rlp.encode(value)
            storage[slot_as_key] = encoded_value
        else:
            del storage[slot_as_key]
    
        external_smart_contract_storage_roots[smart_contract_address] = storage.root_hash
        
        self.logger.debug("setting smart contract storage for address {} | smart_contract_address {} | slot {} | value {}".format(encode_hex(address), encode_hex(smart_contract_address), slot, value))
        self._set_account(address, account.copy(external_smart_contract_storage_root=external_smart_contract_storage_roots.root_hash))


    def delete_external_smart_contract_storage(self, address: Address, smart_contract_address: Address) -> None:
        validate_canonical_address(address, title="Storage Address")
        validate_canonical_address(smart_contract_address, title="smart_contract_address")

        account = self._get_account(address)
        external_smart_contract_storage_roots = HexaryTrie(self._journaldb, account.external_smart_contract_storage_root)

        try:
            external_smart_contract_storage_roots[smart_contract_address] = BLANK_ROOT_HASH
        except KeyError:
            pass

        self._set_account(address, account.copy(external_smart_contract_storage_root=external_smart_contract_storage_roots.root_hash))


    def get_account_hash(self, address: Address) -> Hash32:
        account = self._get_account(address)
        account_hashable_encoded = rlp.encode(account, sedes=PhotonAccount)
        return keccak(account_hashable_encoded)

    #
    # Internal
    #
    def _decode_and_upgrade_account(self, rlp_account: bytes, address: Address, account_version: int, save_new_account = True) -> PhotonAccount:
        if rlp_account:
            if account_version == self.version:
                account = rlp.decode(rlp_account, sedes=PhotonAccount)
            elif account_version == -1:
                self.logger.debug("Found a depreciated account with address {} that needs upgrading to version {}.".format(
                    encode_hex(address), self.version))
                #we need to upgrade this from the depreciated version
                depreciated_account = rlp.decode(rlp_account, sedes=AccountDepreciated)
                account = PhotonAccount(
                    depreciated_account.nonce,
                    depreciated_account.block_number,
                    depreciated_account.balance,
                    depreciated_account.storage_root,
                    code_hash=depreciated_account.code_hash
                )
                if save_new_account:
                    # remember to also save receivable transactions
                    receivable_transactions = depreciated_account.receivable_transactions
                    self.save_receivable_transactions_if_none_exist(address, receivable_transactions)

                    # Lets immediately set the new account
                    self._set_account(address=address, account=account)
            elif account_version == 0:
                self.logger.debug("Found a boson account with address {} that needs upgrading to version {}.".format(
                    encode_hex(address), self.version))
                boson_account = rlp.decode(rlp_account, sedes=Account)
                account = PhotonAccount(
                    boson_account.nonce,
                    boson_account.block_number,
                    boson_account.balance,
                    boson_account.storage_root,
                    code_hash=boson_account.code_hash
                )
                if save_new_account:
                    # Lets immediately set the new account
                    self._set_account(address=address, account=account)
            else:
                raise ValidationError("The loaded account is from an unknown account version {}. This account db is version {}".format(account_version, self.version))

        else:
            account = PhotonAccount()
        return account


    def _set_account(self, address: Address, account: PhotonAccount) -> None:
        if not isinstance(account, PhotonAccount):
            raise ValidationError("Expected a photon account, but a different version of account was provided.")
        encoded_account = rlp.encode(account, sedes=PhotonAccount)
        account_lookup_key = SchemaV1.make_account_lookup_key(address)
        self._journaldb[account_lookup_key] = encoded_account

        # set the account version
        self._set_account_version(address, self.version)

    #
    # Saving account state at particular account hash
    #

    def save_current_account_with_hash_lookup(self, address: Address) -> None:
        validate_canonical_address(address, title="Address")
        account_hash = self.get_account_hash(address)
        account = self._get_account(address)
        rlp_account = rlp.encode(account, sedes=PhotonAccount)

        lookup_key = SchemaV1.make_account_by_hash_lookup_key(account_hash)
        self.db[lookup_key] = rlp_account

        # set the account version
        self._set_account_version(account_hash, self.version)

    def revert_to_account_from_hash(self, account_hash: Hash32, address: Address) -> None:
        validate_canonical_address(address, title="Address")
        validate_is_bytes(account_hash, title="account_hash")
        lookup_key = SchemaV1.make_account_by_hash_lookup_key(account_hash)
        try:
            rlp_encoded = self.db[lookup_key]
            account_version = self._get_account_version(account_hash)
            account = self._decode_and_upgrade_account(rlp_encoded, address, account_version)
            self._set_account(address, account)
        except KeyError:
            raise StateRootNotFound()