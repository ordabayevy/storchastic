from abc import ABC, abstractmethod
import torch
import storch
from storch.tensor import StochasticTensor, CostTensor


class Baseline(ABC, torch.nn.Module):
    def __init__(self):
        super().__init__()

    @abstractmethod
    def compute_baseline(
        self, tensor: StochasticTensor, cost_node: CostTensor
    ) -> torch.Tensor:
        pass


class MovingAverageBaseline(Baseline):
    """
    Takes the (unconditional) average over the different costs. This is not as precise as a conditional baseline.
    """

    def __init__(self, exponential_decay=0.95):
        super().__init__()
        self.register_buffer("exponential_decay", torch.tensor(exponential_decay))
        self.register_buffer("moving_average", torch.tensor(0.0))

    def compute_baseline(
        self, tensor: StochasticTensor, cost_node: CostTensor
    ) -> torch.Tensor:
        avg_cost = storch.reduce_plates(cost_node).detach()
        self.moving_average = (
            self.exponential_decay * self.moving_average
            + (1 - self.exponential_decay) * avg_cost
        )
        return self.moving_average


class BatchAverageBaseline(Baseline):
    # Uses the means of the other samples
    # TODO: Doesn't currently supported non-mean weighted plates.
    def compute_baseline(
        self, tensor: StochasticTensor, costs: CostTensor
    ) -> torch.Tensor:
        if tensor.n == 1:
            raise ValueError(
                "Can only use the batch average baseline if multiple samples are used."
            )
        costs = costs.detach()
        sum_costs = storch.sum(costs, tensor.name)
        baseline = (sum_costs - costs) / (tensor.n - 1)
        return baseline
