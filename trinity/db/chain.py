# Typeshed definitions for multiprocessing.managers is incomplete, so ignore them for now:
# https://github.com/python/typeshed/blob/85a788dbcaa5e9e9a62e55f15d44530cd28ba830/stdlib/3/multiprocessing/managers.pyi#L3
from multiprocessing.managers import (  # type: ignore
    BaseProxy,
)

from trinity.utils.mp import (
    async_method,
    sync_method,
)


class ChainDBProxy(BaseProxy):
    coro_get_block_header_by_hash = async_method('get_block_header_by_hash')
    coro_get_canonical_head = async_method('get_canonical_head')
    coro_get_score = async_method('get_score')
    coro_header_exists = async_method('header_exists')
    coro_get_canonical_block_hash = async_method('get_canonical_block_hash')
    coro_get_canonical_block_header_by_number = async_method('get_canonical_block_header_by_number')
    coro_persist_header = async_method('persist_header')
    coro_persist_uncles = async_method('persist_uncles')
    coro_persist_trie_data_dict = async_method('persist_trie_data_dict')
    coro_get_block_transactions = async_method('get_block_transactions')
    coro_get_block_uncles = async_method('get_block_uncles')
    coro_get_receipts = async_method('get_receipts')
    coro_get_chain_wallet_address_for_block_hash = async_method('get_chain_wallet_address_for_block_hash')
    coro_get_all_blocks_on_chain = async_method('get_all_blocks_on_chain')
    coro_get_block_by_number = async_method('get_block_by_number')
    coro_min_gas_system_initialization_required = async_method('min_gas_system_initialization_required')
    coro_load_historical_network_tpc_capability = async_method('load_historical_network_tpc_capability')
    coro_load_historical_minimum_gas_price = async_method('load_historical_minimum_gas_price')
    coro_save_historical_minimum_gas_price = async_method('save_historical_minimum_gas_price')
    coro_save_historical_network_tpc_capability = async_method('save_historical_network_tpc_capability')
    coro_load_historical_tx_per_centisecond = async_method('load_historical_tx_per_centisecond')


    get_block_header_by_hash = sync_method('get_block_header_by_hash')
    get_canonical_head = sync_method('get_canonical_head')
    get_score = sync_method('get_score')
    header_exists = sync_method('header_exists')
    get_canonical_block_hash = sync_method('get_canonical_block_hash')
    persist_header = sync_method('persist_header')
    persist_uncles = sync_method('persist_uncles')
    persist_trie_data_dict = sync_method('persist_trie_data_dict')
    get_chain_wallet_address_for_block_hash = sync_method('get_chain_wallet_address_for_block_hash')
    get_block_by_number = sync_method('get_block_by_number')
    get_all_blocks_on_chain = sync_method('get_all_blocks_on_chain')
    get_chain_wallet_address_for_block = sync_method('get_chain_wallet_address_for_block')
    min_gas_system_initialization_required = sync_method('min_gas_system_initialization_required')
    load_historical_network_tpc_capability = sync_method('load_historical_network_tpc_capability')
    load_historical_minimum_gas_price = sync_method('load_historical_minimum_gas_price')
    save_historical_minimum_gas_price = sync_method('save_historical_minimum_gas_price')
    save_historical_network_tpc_capability = sync_method('save_historical_network_tpc_capability')
    load_historical_tx_per_centisecond = sync_method('load_historical_tx_per_centisecond')