# Copyright 2021 MosaicML. All Rights Reserved.

from typing import Any, Mapping, Optional, Union

import torch
from sklearn.metrics import f1_score
from torch import Tensor
from torchmetrics import Metric

from composer.models.loss import soft_cross_entropy


# TODO (Moin): write tests for this!
class MaskedAccuracy(Metric):

    def __init__(self, ignore_index, dist_sync_on_step=False):
        # call `self.add_state`for every internal state that is needed for the metrics computations
        # dist_reduce_fx indicates the function that should be used to reduce
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.ignore_index = ignore_index

        self.add_state("correct", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("total", default=torch.tensor(0), dist_reduce_fx="sum")

    def update(self, preds: torch.Tensor, target: torch.Tensor):
        # update metric states
        preds = torch.argmax(preds, dim=-1)
        assert preds.shape == target.shape

        mask = target != self.ignore_index
        masked_target = target[mask]
        masked_preds = preds[mask]

        self.correct += torch.sum(masked_preds == masked_target)
        # self.correct += torch.sum(preds == target)
        self.total += mask.sum()

    def compute(self):
        # compute final result
        return self.correct.float() / self.total


# TODO (Moin): write tests for this!
class CrossEntropyLoss(Metric):
    """Computes cross entropy loss.

    Args:
        dist_sync_on_step (bool): Synchronize metric state across processes at
            each forward() before returning the value at the step.

    State:
        sum_loss (float): the sum of the per-example loss in the batch.
        total_batches (float): the number of batches to average across.
    """

    def __init__(self, vocab_size, dist_sync_on_step=False, ignore_index=None):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.vocab_size = vocab_size
        self.ignore_index = ignore_index
        self.loss_fn = torch.nn.CrossEntropyLoss(ignore_index=ignore_index, reduction="sum")
        self.add_state("sum_loss", default=torch.tensor(0.), dist_reduce_fx="sum")
        self.add_state("total_batches", default=torch.tensor(0), dist_reduce_fx="sum")

    def update(self, output: Union[Mapping, Tensor], target: Tensor) -> None:
        """Updates the internal state with results from a new batch.

        Args:
            output (Mapping): The output from the model, which must contain
                either the Tensor or a Mapping type that contains the loss or model logits.
            target (Tensor): A Tensor of ground-truth values to compare against.
        """

        assert isinstance(output, Tensor)
        output = output.view(-1, self.vocab_size)
        target = target.view(-1)
        losses = self.loss_fn(output, target)
        if self.ignore_index:
            mask = target != self.ignore_index
            total_batches = mask.sum()
        else:
            total_batches = target.shape[0]

        self.total_batches += total_batches  #type: ignore (third-party)

        # accmulate loss over all batches
        self.sum_loss += losses

    def compute(self) -> Tensor:
        """Aggregate the state over all processes to compute the metric.

        Returns:
            loss (Tensor): The loss averaged across all batches.
        """
        # Return average loss over entire dataset
        return self.sum_loss / self.total_batches  #type: ignore (third-party)


class LanguageCrossEntropyLoss(Metric):
    """Hugging Face compatible cross entropy loss.

    Args:
        dist_sync_on_step (bool): Synchronize metric state across processes at
            each forward() before returning the value at the step.

    State:
        sum_loss (float): the sum of the per-example loss in the batch.
        total_batches (float): the number of batches to average across.
    """

    def __init__(self, dist_sync_on_step=False):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.add_state("sum_loss", default=torch.tensor(0.), dist_reduce_fx="sum")
        self.add_state("total_batches", default=torch.tensor(0), dist_reduce_fx="sum")

    def update(self, output: Union[Mapping, Tensor], target: Tensor) -> None:
        """Updates the internal state with results from a new batch.

        Args:
            output (Mapping): The output from the model, which must contain
                either the Tensor or a Mapping type that contains the loss or model logits.
            target (Tensor): A Tensor of ground-truth values to compare against.
        """

        # if logit modification algorithms aren't on, we take the loss directly from the model output
        if isinstance(output, Mapping) and 'loss' in output:
            loss = output['loss']
        else:
            if isinstance(output, Mapping):
                logits = output['logits']
            # recompute the loss on our own
            elif isinstance(output, Tensor):
                logits = output
            else:
                raise Exception(f"Type {type(output)} for the output is unsupported.")

            loss = soft_cross_entropy(logits, target)

        # accmulate loss over all batches
        self.sum_loss += loss

        self.total_batches += 1  #type: ignore (third-party)

    def compute(self) -> Tensor:
        """Aggregate the state over all processes to compute the metric.

        Returns:
            loss (Tensor): The loss averaged across all batches.
        """
        # Return average loss over entire dataset
        return self.sum_loss / self.total_batches  #type: ignore (third-party)


class Perplexity(LanguageCrossEntropyLoss):
    """Subclasses :class:`LanguageCrossEntropyLoss` to implement perplexity.

    If an algorithm modifies the loss function and it is no longer directly
    provided in the output, then this could be expensive because it'll compute the loss twice.
    """

    def compute(self) -> Tensor:
        """Returns torch.exp() of the LanguageCrossEntropyLoss.
        """
        avg_loss = super().compute()
        return torch.exp(avg_loss)


class BinaryF1Score(Metric):
    """Hugging Face compatible cross entropy loss.

    Args:
        dist_sync_on_step (bool): Synchronize metric state across processes at
            each forward() before returning the value at the step.

    State:
        sum_loss (float): the sum of the per-example loss in the batch.
        total_batches (float): the number of batches to average across.
    """

    def __init__(self, dist_sync_on_step=False):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.add_state("predictions", default=[], dist_reduce_fx="cat")
        self.add_state("labels", default=[], dist_reduce_fx="cat")

    def update(self, output: Union[Mapping, Tensor], target: Tensor) -> None:
        """Updates the internal state with results from a new batch.

        Args:
            output (Mapping): The output from the model, which must contain
                either the Tensor or a Mapping type that contains the loss or model logits.
            target (Tensor): A Tensor of ground-truth values to compare against.
        """
        self.predictions.append(output)
        self.labels.append(target)

    def compute(self) -> Tensor:
        """Aggregate the state over all processes to compute the metric.

        Returns:
            loss (Tensor): The loss averaged across all batches.
        """
        # Return average loss over entire dataset
        predictions = torch.argmax(self.predictions, dim=1).cpu()
        labels = self.labels.cpu()
        return float(f1_score(y_pred=predictions, y_true=labels))
