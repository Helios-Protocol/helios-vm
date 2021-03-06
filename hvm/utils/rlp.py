from __future__ import absolute_import

import rlp_cython as rlp

from cytoolz import (
    curry,
)

from eth_utils import (
    to_tuple,
)
from eth_utils import decode_hex
from hvm.exceptions import ValidationError


@to_tuple
def diff_rlp_object(left, right):
    if left != right:
        rlp_type = type(left)

        for field_name, field_type in rlp_type._meta.fields:
            left_value = getattr(left, field_name)
            right_value = getattr(right, field_name)
            if isinstance(field_type, type) and issubclass(field_type, rlp.Serializable):
                sub_diff = diff_rlp_object(left_value, right_value)
                for sub_field_name, sub_left_value, sub_right_value in sub_diff:
                    yield (
                        "{0}.{1}".format(field_name, sub_field_name),
                        sub_left_value,
                        sub_right_value,
                    )
            elif isinstance(field_type, (rlp.sedes.List, rlp.sedes.CountableList)):
                if tuple(left_value) != tuple(right_value):
                    yield (
                        field_name,
                        left_value,
                        right_value,
                    )
            elif left_value != right_value:
                yield (
                    field_name,
                    left_value,
                    right_value,
                )
            else:
                continue


@curry
def ensure_rlp_objects_are_equal(obj_a, obj_b, obj_a_name, obj_b_name):
    if obj_a == obj_b:
        return
    diff = diff_rlp_object(obj_a, obj_b)
    longest_field_name = max(len(field_name) for field_name, _, _ in diff)
    error_message = (
        "Mismatch between {obj_a_name} and {obj_b_name} on {0} fields:\n - {1}".format(
            len(diff),
            "\n - ".join(tuple(
                "{0}:\n    (actual)  : {1}\n    (expected): {2}".format(
                    field_name.ljust(longest_field_name, ' '),
                    actual,
                    expected,
                )
                for field_name, actual, expected
                in diff
            )),
            obj_a_name=obj_a_name,
            obj_b_name=obj_b_name,
        )
    )
    raise ValidationError(error_message)

@curry
def validate_rlp_equal(obj_a,
                       obj_b,
                       obj_a_name: str=None,
                       obj_b_name: str=None) -> None:
    if obj_a == obj_b:
        return

    if obj_a_name is None:
        obj_a_name = obj_a.__class__.__name__ + '_a'
    if obj_b_name is None:
        obj_b_name = obj_b.__class__.__name__ + '_b'

    diff = diff_rlp_object(obj_a, obj_b)
    if len(diff) == 0:
        raise TypeError(
            "{} ({!r}) != {} ({!r}) but got an empty diff".format(
                obj_a_name,
                obj_a,
                obj_b_name,
                obj_b,
            )
        )
    longest_field_name = max(len(field_name) for field_name, _, _ in diff)
    error_message = (
        "Mismatch between {obj_a_name} and {obj_b_name} on {0} fields:\n - {1}".format(
            len(diff),
            "\n - ".join(tuple(
                "{0}:\n    (actual)  : {1}\n    (expected): {2}".format(
                    field_name.ljust(longest_field_name, ' '),
                    actual,
                    expected,
                )
                for field_name, actual, expected
                in diff
            )),
            obj_a_name=obj_a_name,
            obj_b_name=obj_b_name,
        )
    )
    raise ValidationError(error_message)

ensure_imported_block_unchanged = ensure_rlp_objects_are_equal(
    obj_a_name="block",
    obj_b_name="imported block",
)

def make_mutable(value, force_deep_check = False):
    if force_deep_check:
        if isinstance(value, tuple) or isinstance(value, list):
            return list(make_mutable(item) for item in value)
        else:
            return value
    else:
        if isinstance(value, tuple):
            return list(make_mutable(item) for item in value)
        else:
            return value
    
def convert_rlp_to_correct_class(wanted_class, given_object):

    given_object_parameter_names = set(dict(given_object._meta.fields).keys())
    wanted_class_parameter_names = set(dict(wanted_class._meta.fields).keys())
    parameter_names = list(given_object_parameter_names.intersection(wanted_class_parameter_names))

    dict_params = {}
    for parameter_name in parameter_names:
        dict_params[parameter_name] = getattr(given_object, parameter_name)

    new_object = wanted_class(**dict_params)
    return new_object

