from hvm import constants
from hvm.exceptions import DepreciatedVMFunctionality


def blockhash(computation):
    # block_number = computation.stack_pop(type_hint=constants.UINT256)
    #
    # block_hash = computation.state.get_ancestor_hash(block_number)
    #
    # computation.stack_push(block_hash)
    raise DepreciatedVMFunctionality("blockhash has been removed.")


def coinbase(computation):
    computation.stack_push(computation.state.coinbase)


def timestamp(computation):
    computation.stack_push(computation.state.timestamp)


def number(computation):
    computation.stack_push(computation.state.block_number)


def difficulty(computation):
    computation.stack_push(computation.state.difficulty)


def gaslimit(computation):
    computation.stack_push(computation.state.gas_limit)
