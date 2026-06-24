# decisions

# Concepts
* [Adopt data-olympus for the knowledge base](ADR-001-use-data-olympus.md) - Acme stores its knowledge as a data-olympus bundle.
* [Use single-writer MCP serving model](ADR-002-single-writer-serving.md) - Acme runs one write-enabled data-olympus MCP replica to prevent concurrent-write races on the knowledge bundle.
