from cytoolz import (
    curry,
)

from sortedcontainers import SortedDict

ZERO_BYTE = b'\x00'


@curry
def zpad_right(value: bytes, to_size: int) -> bytes:
    return value.ljust(to_size, ZERO_BYTE)


@curry
def zpad_left(value: bytes, to_size: int) -> bytes:
    return value.rjust(to_size, ZERO_BYTE)


pad32 = zpad_left(to_size=32)
pad32r = zpad_right(to_size=32)

def de_sparse_timestamp_item_list(sparse_list, spacing, filler = None):
    if len(sparse_list) <= 1:
        return sparse_list
    
    start_timestamp = sparse_list[0][0]
    end_timestamp = sparse_list[-1][0]
    
    expected_length = (end_timestamp-start_timestamp)/spacing + abs(spacing)
    
    if len(sparse_list) == expected_length:
        return sparse_list
    
    sparse_dict = SortedDict(sparse_list)
    for timestamp in range(start_timestamp, end_timestamp+spacing, spacing):
        if timestamp not in sparse_dict:
            if filler is not None:
                sparse_dict[timestamp] = filler
            else:
                sparse_dict[timestamp] = sparse_dict[timestamp-spacing]
            
    
    return list(sparse_dict.items())
