"""Registry for discovering and managing IntentProcessors.

The ProcessorRegistry is the central place where IntentProcessors are
registered and looked up. Validators use it to find the right processor
for each intent type; miners register their processors here to make them
available for intent processing.

Usage::

    registry = ProcessorRegistry()

    # Register processors
    registry.register(SwapIntentProcessor())
    registry.register(LimitOrderProcessor())

    # Look up a processor for a given intent type
    processor = registry.get_processor("swap")
    if processor:
        plan = await processor.generate_plan(intent, state, context)
"""

from __future__ import annotations

from minotaur_subnet.sdk.intent_processor import IntentProcessor


class ProcessorRegistry:
    """Registry of available IntentProcessors.

    Validators use this to find the right processor for each intent type.
    Miners register their processors here.

    When multiple processors support the same intent type, the most recently
    registered one takes priority (last-write-wins). This allows miners to
    override the default solver with their own implementation.
    """

    def __init__(self) -> None:
        # Maps intent_type -> list of processors (most recent last)
        self._processors: dict[str, list[IntentProcessor]] = {}

    def register(self, processor: IntentProcessor) -> None:
        """Register a processor for its supported intent types.

        The processor's supported_intent_types() method is called to
        determine which intent types it handles. It is registered under
        each of those types.

        Args:
            processor: The IntentProcessor instance to register.

        Raises:
            ValueError: If the processor supports no intent types.
        """
        types = processor.supported_intent_types()
        if not types:
            raise ValueError(
                f"Processor {type(processor).__name__} declares no supported "
                f"intent types. Override supported_intent_types() to return "
                f"at least one type."
            )

        for intent_type in types:
            if intent_type not in self._processors:
                self._processors[intent_type] = []
            self._processors[intent_type].append(processor)

    def unregister(self, processor: IntentProcessor) -> None:
        """Remove a processor from the registry.

        Args:
            processor: The IntentProcessor instance to remove.
        """
        for intent_type in processor.supported_intent_types():
            if intent_type in self._processors:
                self._processors[intent_type] = [
                    p for p in self._processors[intent_type] if p is not processor
                ]
                # Clean up empty lists
                if not self._processors[intent_type]:
                    del self._processors[intent_type]

    def get_processor(self, intent_type: str) -> IntentProcessor | None:
        """Get the best processor for an intent type.

        Returns the most recently registered processor for the given type,
        or None if no processor is registered for that type.

        Args:
            intent_type: The intent type to look up (e.g. "swap").

        Returns:
            The IntentProcessor to use, or None if none registered.
        """
        processors = self._processors.get(intent_type)
        if not processors:
            return None
        # Last registered wins (miners can override defaults)
        return processors[-1]

    def get_all_processors(self, intent_type: str) -> list[IntentProcessor]:
        """Get all registered processors for an intent type.

        Args:
            intent_type: The intent type to look up.

        Returns:
            List of all processors for that type, ordered by registration
            time (oldest first). Returns empty list if none registered.
        """
        return list(self._processors.get(intent_type, []))

    def list_processors(self) -> dict[str, list[IntentProcessor]]:
        """List all registered processors grouped by intent type.

        Returns:
            Dictionary mapping intent type strings to lists of processors.
        """
        return {k: list(v) for k, v in self._processors.items()}

    def supported_types(self) -> list[str]:
        """Return all intent types that have at least one registered processor.

        Returns:
            Sorted list of intent type strings.
        """
        return sorted(self._processors.keys())

    def __contains__(self, intent_type: str) -> bool:
        """Check if an intent type has a registered processor."""
        return intent_type in self._processors and len(self._processors[intent_type]) > 0

    def __len__(self) -> int:
        """Return the total number of registered processor instances."""
        return sum(len(v) for v in self._processors.values())
