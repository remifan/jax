# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from functools import partial

import numpy as np

from jax import lax
from jax import core
from jax import numpy as jnp
from jax import tree_util
from jax._src.api import jit, vmap
from jax.lib import xla_bridge
from jax.lib import xla_client
from jax.lib import cuda_prng
from jax.interpreters import batching
from jax.interpreters import xla
from jax._src.util import prod


UINT_DTYPES = {8: jnp.uint8, 16: jnp.uint16, 32: jnp.uint32, 64: jnp.uint64}


@tree_util.register_pytree_node_class
class PRNGKey:
  """Represents a PRNG key or batch thereof."""

  key: jnp.ndarray

  def __init__(self, key: jnp.ndarray):
    # key might be a dummy object due to tree_unflatten
    ndim = getattr(key, 'ndim', 1)
    dtype = getattr(key, 'dtype', np.uint32)
    if ndim < 1 or dtype != np.uint32:
      raise TypeError(
          f'invalid prng key or key batch: ndim = {ndim}, dtype = {dtype}')
    self.key = key

  def tree_flatten(self):
    return (self.key,), None

  @classmethod
  def tree_unflatten(cls, _, key):
    key, = key
    return cls(key)

  def fold_in(self, data: int) -> 'PRNGKey':
    return PRNGKey(_fold_in(self.key, data))

  def random_bits(self, bit_width, shape) -> jnp.ndarray:
    return _random_bits(self.key, bit_width, shape)

  def split(self, num: int) -> 'PRNGKey':
    return PRNGKey(_split(self.key, num))

  def __iter__(self):
    assert self.key.ndim > 0
    if self.key.ndim == 1:
      raise TypeError('iteration over a single PRNG key')
    return (PRNGKey(k) for k in self.key.__iter__())

  # TODO(frostig): remove if possible, otherwise declare necessary
  @property
  def shape(self):
    return self.key.shape

  # TODO(frostig): remove if possible, otherwise declare necessary
  @property
  def dtype(self):
    return self.key.dtype


def make_prng_key(seed: int) -> PRNGKey:
  return PRNGKey(_threefry_prng_key(seed))


def _threefry_prng_key(seed: int) -> jnp.ndarray:
  """Create a pseudo-random number generator (PRNG) key given an integer seed.

  Args:
    seed: a 64- or 32-bit integer used as the value of the key.

  Returns:
    A PRNG key, which is modeled as an array of shape (2,) and dtype uint32. The
    key is constructed from a 64-bit seed by effectively bit-casting to a pair
    of uint32 values (or from a 32-bit seed by first padding out with zeros).
  """
  # Avoid overflowerror in X32 mode by first converting ints to int64.
  # This breaks JIT invariance of PRNGKey for large ints, but supports the
  # common use-case of instantiating PRNGKey with Python hashes in X32 mode.
  if isinstance(seed, int):
    seed_arr = jnp.asarray(np.int64(seed))
  else:
    seed_arr = jnp.asarray(seed)
  if seed_arr.shape:
    raise TypeError(f"PRNGKey seed must be a scalar; got {seed!r}.")
  if not np.issubdtype(seed_arr.dtype, np.integer):
    raise TypeError(f"PRNGKey seed must be an integer; got {seed!r}")

  convert = lambda k: lax.reshape(lax.convert_element_type(k, np.uint32), [1])
  k1 = convert(lax.shift_right_logical(seed_arr, lax._const(seed_arr, 32)))
  k2 = convert(jnp.bitwise_and(seed_arr, np.uint32(0xFFFFFFFF)))
  return lax.concatenate([k1, k2], 0)


def _is_prng_key(key: jnp.ndarray) -> bool:
  try:
    return key.shape == (2,) and key.dtype == np.uint32
  except AttributeError:
    return False

def _make_rotate_left(dtype):
  if not jnp.issubdtype(dtype, np.integer):
    raise TypeError("_rotate_left only accepts integer dtypes.")
  nbits = np.array(jnp.iinfo(dtype).bits, dtype)

  def _rotate_left(x, d):
    if lax.dtype(d) != dtype:
      d = lax.convert_element_type(d, dtype)
    if lax.dtype(x) != dtype:
      x = lax.convert_element_type(x, dtype)
    return lax.shift_left(x, d) | lax.shift_right_logical(x, nbits - d)
  return _rotate_left


def _bit_stats(bits):
  """This is a debugging function to compute the statistics of bit fields."""
  return np.array([list(map(int, np.binary_repr(x, 64))) for x in bits]).mean(0)


### hash function and split

def _threefry2x32_abstract_eval(*args):
  if any(a.dtype != jnp.uint32 for a in args):
    raise TypeError("Arguments to threefry2x32 must have uint32 type, got {}"
                    .format(args))
  if all(isinstance(arg, core.ShapedArray) for arg in args):
    shape = lax._broadcasting_shape_rule(*args)
    named_shape = core.join_named_shapes(*(a.named_shape for a in args))
    aval = core.ShapedArray(shape, jnp.dtype(jnp.uint32), named_shape=named_shape)
  else:
    aval = core.UnshapedArray(jnp.dtype(jnp.uint32))
  return (aval,) * 2


rotate_left = _make_rotate_left(np.uint32)


def apply_round(v, rot):
  v = v[:]
  v[0] = v[0] + v[1]
  v[1] = rotate_left(v[1], rot)
  v[1] = v[0] ^ v[1]
  return v


def rotate_list(xs):
  return xs[1:] + xs[:1]


def rolled_loop_step(i, state):
  x, ks, rotations = state
  for r in rotations[0]:
    x = apply_round(x, r)
  new_x = [x[0] + ks[0], x[1] + ks[1] + jnp.asarray(i + 1, dtype=np.uint32)]
  return new_x, rotate_list(ks), rotate_list(rotations)


def _threefry2x32_lowering(key1, key2, x1, x2, use_rolled_loops=True):
  """Apply the Threefry 2x32 hash.

  Args:
    keypair: a pair of 32bit unsigned integers used for the key.
    count: an array of dtype uint32 used for the counts.

  Returns:
    An array of dtype uint32 with the same shape as `count`.
  """
  x = [x1, x2]

  rotations = [np.array([13, 15, 26, 6], dtype=np.uint32),
               np.array([17, 29, 16, 24], dtype=np.uint32)]
  ks = [key1, key2, key1 ^ key2 ^ np.uint32(0x1BD11BDA)]

  x[0] = x[0] + ks[0]
  x[1] = x[1] + ks[1]

  if use_rolled_loops:
    x, _, _ = lax.fori_loop(0, 5, rolled_loop_step, (x, rotate_list(ks), rotations))

  else:
    for r in rotations[0]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[1]
    x[1] = x[1] + ks[2] + np.uint32(1)

    for r in rotations[1]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[2]
    x[1] = x[1] + ks[0] + np.uint32(2)

    for r in rotations[0]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[0]
    x[1] = x[1] + ks[1] + np.uint32(3)

    for r in rotations[1]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[1]
    x[1] = x[1] + ks[2] + np.uint32(4)

    for r in rotations[0]:
      x = apply_round(x, r)
    x[0] = x[0] + ks[2]
    x[1] = x[1] + ks[0] + np.uint32(5)

  return tuple(x)


def _threefry2x32_gpu_translation_rule(c, k1, k2, x1, x2):
  shape = lax.broadcast_shapes(
      c.get_shape(k1).dimensions(), c.get_shape(k2).dimensions(),
      c.get_shape(x1).dimensions(), c.get_shape(x2).dimensions())
  rank = len(shape)
  if 0 in shape:
    zeros = xla_client.ops.Broadcast(
        xla_bridge.constant(c, np.array(0, np.uint32)), shape)
    return xla_client.ops.Tuple(c, [zeros, zeros])
  def _broadcast(x):
    ndims = c.get_shape(x).rank()
    return xla_client.ops.BroadcastInDim(x, shape,
                                         tuple(range(rank - ndims, rank)))
  return cuda_prng.threefry2x32(
      c, (_broadcast(k1), _broadcast(k2)), (_broadcast(x1), _broadcast(x2)))


threefry2x32_p = core.Primitive("threefry2x32")
threefry2x32_p.multiple_results = True
threefry2x32_p.def_impl(partial(xla.apply_primitive, threefry2x32_p))
threefry2x32_p.def_abstract_eval(_threefry2x32_abstract_eval)
batching.defbroadcasting(threefry2x32_p)
xla.translations_with_avals[threefry2x32_p] = xla.lower_fun(
    partial(_threefry2x32_lowering, use_rolled_loops=False),
    multiple_results=True, with_avals=True)
xla.backend_specific_translations['cpu'][threefry2x32_p] = xla.lower_fun(
    partial(_threefry2x32_lowering, use_rolled_loops=True),
    multiple_results=True)
if cuda_prng:
  xla.backend_specific_translations['gpu'][threefry2x32_p] = \
      _threefry2x32_gpu_translation_rule


@jit
def threefry_2x32(keypair, count):
  """Apply the Threefry 2x32 hash.

  Args:
    keypair: a pair of 32bit unsigned integers used for the key.
    count: an array of dtype uint32 used for the counts.

  Returns:
    An array of dtype uint32 with the same shape as `count`.
  """
  key1, key2 = keypair
  if not lax.dtype(key1) == lax.dtype(key2) == lax.dtype(count) == np.uint32:
    msg = "threefry_2x32 requires uint32 arguments, got {}"
    raise TypeError(msg.format([lax.dtype(x) for x in [key1, key2, count]]))

  odd_size = count.size % 2
  if odd_size:
    x = list(jnp.split(jnp.concatenate([count.ravel(), np.uint32([0])]), 2))
  else:
    x = list(jnp.split(count.ravel(), 2))

  x = threefry2x32_p.bind(key1, key2, x[0], x[1])
  out = jnp.concatenate(x)
  assert out.dtype == np.uint32
  return lax.reshape(out[:-1] if odd_size else out, count.shape)


def _split(key: jnp.ndarray, num: int) -> jnp.ndarray:
  return _threefry_split(key, int(num))  # type: ignore


@partial(jit, static_argnums=(1,))
def _threefry_split(key, num) -> jnp.ndarray:
  counts = lax.iota(np.uint32, num * 2)
  return lax.reshape(threefry_2x32(key, counts), (num, 2))


def _fold_in(key: jnp.ndarray, data: int) -> jnp.ndarray:
  return _threefry_fold_in(key, jnp.uint32(data))


@jit
def _threefry_fold_in(key, data):
  return threefry_2x32(key, _threefry_prng_key(data))


@partial(jit, static_argnums=(1, 2))
def _random_bits(key: jnp.ndarray, bit_width, shape):
  """Sample uniform random bits of given width and shape using PRNG key."""
  if not _is_prng_key(key):
    raise TypeError("_random_bits got invalid prng key.")
  if bit_width not in (8, 16, 32, 64):
    raise TypeError("requires 8-, 16-, 32- or 64-bit field width.")
  shape = core.as_named_shape(shape)
  for name, size in shape.named_items:
    real_size = lax.psum(1, name)
    if real_size != size:
      raise ValueError(f"The shape of axis {name} was specified as {size}, "
                       f"but it really is {real_size}")
    axis_index = lax.axis_index(name)
    key = _fold_in(key, axis_index)
  size = prod(shape.positional)
  max_count = int(np.ceil(bit_width * size / 32))

  nblocks, rem = divmod(max_count, jnp.iinfo(np.uint32).max)
  if not nblocks:
    bits = threefry_2x32(key, lax.iota(np.uint32, rem))
  else:
    keys = _split(key, nblocks + 1)
    subkeys, last_key = keys[:-1], keys[-1]
    blocks = vmap(threefry_2x32, in_axes=(0, None))(subkeys, lax.iota(np.uint32, jnp.iinfo(np.uint32).max))
    last = threefry_2x32(last_key, lax.iota(np.uint32, rem))
    bits = lax.concatenate([blocks.ravel(), last], 0)

  dtype = UINT_DTYPES[bit_width]
  if bit_width == 64:
    bits = [lax.convert_element_type(x, dtype) for x in jnp.split(bits, 2)]
    bits = lax.shift_left(bits[0], dtype(32)) | bits[1]
  elif bit_width in [8, 16]:
    # this is essentially bits.view(dtype)[:size]
    bits = lax.bitwise_and(
      np.uint32(np.iinfo(dtype).max),
      lax.shift_right_logical(
        lax.broadcast(bits, (1,)),
        lax.mul(
          np.uint32(bit_width),
          lax.broadcasted_iota(np.uint32, (32 // bit_width, 1), 0)
        )
      )
    )
    bits = lax.reshape(bits, (np.uint32(max_count * 32 // bit_width),), (1, 0))
    bits = lax.convert_element_type(bits, dtype)[:size]
  return lax.reshape(bits, shape)
