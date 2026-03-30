"""Abstract base class for all execution backends."""

from abc import ABC, abstractmethod


class Backend(ABC):
    @abstractmethod
    def execute_node(self, node_id, node, state):
        """Execute a single workflow node. Return a dict conforming to spec/node-contract.md."""
        ...
