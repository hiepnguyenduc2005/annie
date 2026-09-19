# Shared protocol definitions

`messages.py` defines the proposed robot-to-app signal contract using Pydantic.
Neither backend currently imports it: each backend can start with only its own
folder and requirements. Before implementing transport, publish this contract
as a versioned package or generate local validators from a versioned schema.
Do not introduce runtime imports from sibling backend folders.
