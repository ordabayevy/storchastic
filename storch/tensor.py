from __future__ import annotations
import torch
import storch
from torch.distributions import Distribution
from collections import deque
from typing import Union, List, Tuple, Dict, Iterable, Any, Callable
import builtins
from itertools import product
from typing import Optional
from torch import dtype, device, layout, strided, Size

# from storch.typing import BatchTensor

_int = builtins.int
_size = Union[Size, List[_int], Tuple[_int, ...]]

_torch_dict = None

Plate = Tuple[str, int]

class Tensor(torch.Tensor):
    def __init__(self, tensor: torch.Tensor, parents: [Tensor],
                 batch_links: [Plate], name: Optional[str] = None):
        plate_names = set()
        batch_len = 0
        # Check whether this tensor does not violate the constraints imposed by the given batch_links
        for plate in batch_links:
            plate_name, plate_n = plate
            if plate_name in plate_names:
                raise ValueError("Batch links contain two instances of plate " + plate_name + ". This can be caused by "
                                 "different samples with the same name using a different amount of samples n. "
                                 "Make sure that these samples use the same number of samples.")
            plate_names.add(plate_name)
            if plate_n == 1:  # plate length is 1. Ignore this dimension, as singleton dimensions should not exist.
                continue
            if len(tensor.shape) <= batch_len:
                raise ValueError(
                    "Got an input tensor with a shape too small for its surrounding batch. Violated at dimension "
                    + str(batch_len) + " and plate shape dimension " + str(len(batch_links)) + ". Instead, it was " + str(
                        len(tensor.shape)))
            elif not tensor.shape[batch_len] == plate_n:
                raise ValueError(
                    "Storch Tensors should take into account their surrounding plates. Violated at dimension " + str(batch_len)
                    + " and plate " + plate_name + " with size " + str(plate_n) + ". "
                    "Instead, it was " + str(tensor.shape[batch_len]) + ". Batch links: " + str(batch_links) + " Tensor shape: "
                    + str(tensor.shape))
            batch_len += 1

        self._name = name
        self._tensor = tensor
        self._parents = []
        for p in parents:
            if p.is_cost:
                raise ValueError("Cost nodes cannot have children.")
            differentiable_link = has_backwards_path(self, p)
            self._parents.append((p, differentiable_link))
            p._children.append((self, differentiable_link))
        self._children = []
        self.batch_len = batch_len
        self.event_shape = tensor.shape[batch_len:]
        self.batch_links = batch_links

    def __getattribute__(self, name):
        # TODO: We can possibly improve the performance of this method by precomputing the wrappers.
        # Loop over the dict, and for missing values, wrap the callable.

        # Note that __getattribute__ does not work for magic methods like __add__
        # print("Trying to get", name)
        if not storch.tensor._torch_dict:
            storch.tensor._torch_dict = dir(torch.Tensor)
        if name != "__dict__" and name != "__class__" and name not in Tensor.__dict__ \
                and name in storch.tensor._torch_dict:
            attr = getattr(Tensor, name)
            if storch._debug:
                print("Wrapping tensor function", name)
            if isinstance(attr, Callable):
                return storch.wrappers._self_deterministic(attr, self)
            raise AttributeError(name)
        else:
            return super().__getattribute__(name)

    def __dir__(self):
        tensor_methods = dir(self.__class__)
        attrs = list(self.__dict__.keys())
        keys = tensor_methods + attrs

        return sorted(keys)

    @staticmethod
    def __new__(cls, *args, **kwargs):
        # print("here")
        tensor = args[0]
        try:
            # Pass the input tensor to register this tensor in C. Or something.
            if tensor.ndimension() > 0:
                return super(torch.Tensor, cls).__new__(cls, tensor)
        except TypeError as e:
            if storch._debug:
                print("Was not able to create the object using the input tensor. Using a fallback construction.")
                print("TypeError:", e)

        # For some reason, scalar tensors cannot be used in the __new__ constructor? That's when these type errors could
        # happen. It can also happen with eg bool tensors. Passing the device is still useful
        return super(torch.Tensor, cls).__new__(cls, device=args[0].device)  # args[0])

    def new_tensor(self, data: Any, dtype: Optional[dtype] = None, device: Union[device, str, None] = None,
                   requires_grad: bool = False) -> Tensor:
        return self._tensor.new_tensor(data, dtype, device, requires_grad)

    def new_full(self, size: _size, fill_value: torch.Number, *, dtype: dtype = None, layout: layout = strided,
                 device: Union[device, str, None] = None, requires_grad: bool = False) -> Tensor:
        return self._tensor.new_full(size, fill_value, dtype=dtype, layout=layout, device=device,
                                     requires_grad=requires_grad)

    def new_empty(self, size: _size, *, dtype: dtype = None, layout: layout = strided,
                  device: Union[device, str, None] = None, requires_grad: bool = False) -> Tensor:
        return self._tensor.new_empty(size, dtype=dtype, layout=layout, device=device, requires_grad=requires_grad)

    def new_ones(self, size: _size, dtype: Optional[dtype] = None, device: Union[device, str, None] = None,
                 requires_grad: bool = False) -> Tensor:
        return self._tensor.new_ones(size, dtype, device, requires_grad)

    def new_zeros(self, size: _size, *, dtype: dtype = None, layout: layout = strided,
                  device: Union[device, str, None] = None, requires_grad: bool = False) -> Tensor:
        return self._tensor.new_zeros(size, dtype=dtype, layout=layout, device=device, requires_grad=requires_grad)

    @storch.deterministic
    def __eq__(self, other):
        return self.__eq__(other)

    def __hash__(self):
        return object.__hash__(self)

    @property
    def name(self):
        return self._name

    @property
    def is_sparse(self):
        return self._tensor.is_sparse

    def __str__(self):
        t = (self.name + ": " if self.name else "") + \
            "Stochastic" if self.stochastic else ("Cost" if self.is_cost else "Deterministic")
        return t + " " + str(self._tensor) + " Batch links: " + str(self.batch_links)

    def __repr__(self):
        return object.__repr__(self)

    def _walk(self, expand_fn, depth_first=True, only_differentiable=False, repeat_visited=False, walk_fn=lambda x: x):
        visited = set()
        if depth_first:
            S = [self]
            while S:
                v = S.pop()
                if repeat_visited or v not in visited:
                    yield walk_fn(v)
                    visited.add(v)
                    for w, d in expand_fn(v):
                        if d or not only_differentiable:
                            S.append(w)
        else:
            queue = deque()
            visited.add(self)
            queue.append(self)
            while queue:
                v = queue.popleft()
                yield walk_fn(v)
                for w, d in expand_fn(v):
                    if (repeat_visited or w not in visited) and (d or not only_differentiable):
                        visited.add(w)
                        queue.append(w)

    def walk_parents(self, depth_first=True, only_differentiable=False, repeat_visited=False, walk_fn=lambda x: x):
        return self._walk(lambda p: p._parents, depth_first, only_differentiable, repeat_visited, walk_fn)

    def walk_children(self, depth_first=True, only_differentiable=False, repeat_visited=False, walk_fn=lambda x: x):
        return self._walk(lambda p: p._children, depth_first, only_differentiable, repeat_visited, walk_fn)

    def detach_tensor(self) -> torch.Tensor:
        return self._tensor.detach()

    @property
    def stochastic(self) -> bool:
        return False

    @property
    def is_cost(self) -> bool:
        return False

    @property
    def requires_grad(self) -> bool:
        return self._tensor.requires_grad

    @property
    def batch_shape(self) -> torch.Size:
        return self._tensor.shape[:self.batch_len]

    @property
    def shape(self) -> torch.Size:
        return self._tensor.size()

    def size(self) -> torch.Size:
        return self._tensor.size()

    def is_cuda(self):
        return self._tensor.is_cuda

    @property
    def dtype(self):
        return self._tensor.dtype

    @property
    def layout(self):
        return self._tensor.layout

    @property
    def device(self):
        return self._tensor.device

    @property
    def grad(self):
        return self._tensor.grad

    def dim(self):
        return self._tensor.dim()

    def ndimension(self):
        return self._tensor.ndimension()

    def register_hook(self, hook: Callable) -> Any:
        return self._tensor.register_hook(hook)

    def event_dim_indices(self):
        return list(range(self.batch_len, self._tensor.dim()))

    def batch_dim_indices(self):
        return list(range(self.batch_len))

    def iterate_batch_indices(self):
        ranges = list(map(lambda a: list(range(a)), self.batch_shape))
        return product(*ranges)

    def multi_dim_plates(self) -> [Plate]:
        platez = []
        for plate_name, plate_n in self.batch_links:
            if plate_n > 1:
                platez.append((plate_name, plate_n))
        return platez

    # region OperatorOverloads

    @storch.deterministic
    def __getitem__(self, indices: Union[None, _int, slice, Tensor, List, Tuple]):
        # TODO: properly test this
        return self.__getitem__(indices)

    def __index__(self):
        return self._tensor.__index__()

    @storch.deterministic
    def __setitem__(self, key, value):
        return self.__setitem__(key, value)

    @storch.deterministic
    def __add__(self, other):
        return self.__add__(other)

    @storch.deterministic
    def __radd__(self, other):
        return self.__radd__(other)

    @storch.deterministic
    def __sub__(self, other):
        return self.__sub__(other)

    @storch.deterministic
    def __mul__(self, other):
        return self.__mul__(other)

    @storch.deterministic
    def __rmul__(self, other):
        return self.__rmul__(other)

    @storch.deterministic
    def __matmul__(self, other):
        return self.__matmul__(other)

    @storch.deterministic
    def __pow__(self, other):
        return self.__pow__(other)

    @storch.deterministic
    def __div__(self, other):
        return self.__div__(other)

    @storch.deterministic
    def __mod__(self, other):
        return self.__mod__(other)

    @storch.deterministic
    def __truediv__(self, other):
        return self.__truediv__(other)

    @storch.deterministic
    def __floordiv__(self, other):
        return self.__floordiv__(other)

    @storch.deterministic
    def __rfloordiv__(self, other):
        return self.__rfloordiv__(other)

    @storch.deterministic
    def __abs__(self):
        return self.__abs__()

    @storch.deterministic
    def __and__(self, other):
        return self.__and__(other)

    def eq(self, other):
        return self.eq(other)

    @storch.deterministic
    def __ge__(self, other):
        return self.__ge__(other)

    @storch.deterministic
    def __gt__(self, other):
        return self.__gt__(other)

    @storch.deterministic
    def __invert__(self):
        return self.__invert__()

    @storch.deterministic
    def __le__(self, other):
        return self.__le__(other)

    @storch.deterministic
    def __lshift__(self, other):
        return self.__lshift__(other)

    @storch.deterministic
    def __lt__(self, other):
        return self.__lt__(other)

    @storch.deterministic
    def ne(self, other):
        return self.ne(other)

    @storch.deterministic
    def __neg__(self):
        return self.__neg__()

    @storch.deterministic
    def __or__(self, other):
        return self.__or__(other)

    @storch.deterministic
    def __rshift__(self, other):
        return self.__rshift__(other)

    @storch.deterministic
    def __xor__(self, other):
        return self.__xor__(other)

    def __bool__(self):
        from storch.exceptions import IllegalConditionalError
        raise IllegalConditionalError("It is not allowed to convert storch tensors to boolean. Make sure to unwrap "
                                      "storch tensors to normal torch tensor to use this tensor as a boolean.")
    # endregion


class DeterministicTensor(Tensor):
    def __init__(self, tensor: torch.Tensor, parents, batch_links: [Tuple[str, int]],
                 is_cost: bool, name: Optional[str] = None):
        super().__init__(tensor, parents, batch_links, name)
        self._is_cost = is_cost
        if is_cost and torch.is_grad_enabled():
            storch.inference._cost_tensors.append(self)
            if not name:
                raise ValueError("Added a cost node without providing a name")

    @property
    def is_cost(self) -> bool:
        return self._is_cost


class IndependentTensor(Tensor):
    """
    Used to denote independencies on a Tensor. This could for example be the minibatch dimension. The first dimension
    of the input tensor is taken to be independent and added as a batch dimension to the storch system.
    """

    def __init__(self, tensor: torch.Tensor, parents: [Tensor],
                 batch_links: [Tuple[str, int]], name: str):
        n = tensor.shape[0]
        for plate_name, plate_n in batch_links:
            if plate_name == name:
                raise ValueError(
                    "Cannot create independent tensor with name " + name + ". A parent sample has already used"
                    " this name. Use a different name for this independent dimension.")
        batch_links.insert(0, (name, n))
        super().__init__(tensor, parents, batch_links, name)
        self.n = n


class StochasticTensor(Tensor):
    # TODO: Copy original tensor to make sure it cannot change using inplace
    def __init__(self, tensor: torch.Tensor, parents: [Tensor], sampling_method: storch.Method,
                 batch_links: [Tuple[str, int]],
                 distribution: Distribution, requires_grad: bool, n: int, name: str):
        for plate_name, plate_n in batch_links:
            if plate_name == name:
                raise ValueError("Cannot create stochastic tensor with name " + name + ". A parent sample has already used"
                                " this name. Use a different name for this sample.")
        batch_links.insert(0, (name, n))
        self.n = n
        self.distribution = distribution
        super().__init__(tensor, parents, batch_links, name)
        self.sampling_method = sampling_method
        self._requires_grad = requires_grad
        self._accum_grads = {}
        self._grad = None

    @property
    def stochastic(self):
        return True

    @property
    # TODO: Should not manually override it like this. The stochastic "requires_grad" should be a different method, so
    # that the meaning of requires_grad is consistent everywhere
    def requires_grad(self):
        return self._requires_grad

    @property
    def grad(self):
        return self._accum_grads

    def total_expected_grad(self) -> Dict[str, torch.Tensor]:
        r = {}
        indices = self.batch_dim_indices()
        for name, grad in self._accum_grads.items():
            tensor = getattr(self.distribution, name)
            if grad.dim() == tensor.dim():
                r[name] = grad
            else:
                r[name] = grad.mean(dim=indices)
        return r

    def total_variance_grad(self) -> Dict[str, torch.Tensor]:
        """
        Computes the total variance on the gradient of the parameters of this distribution over all simulations .
        :return:
        """
        r = {}
        indices = self.batch_dim_indices()
        for name, grad in self._accum_grads.items():
            tensor = getattr(self.distribution, name)
            if grad.dim() == tensor.dim():
                raise ValueError("There are no batched dimensions to take statistics over. Make sure to call backwards "
                                 "with accum_grad=True")
            expected = grad.mean(dim=indices)
            diff = grad - expected
            squared_diff = diff * diff
            sse = squared_diff.sum(dim=indices)
            r[name] = sse.mean()
        return r


from storch.util import has_backwards_path
