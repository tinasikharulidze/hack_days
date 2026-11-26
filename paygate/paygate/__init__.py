"""paygate: the layer between an AI agent and a bank.

An agent talks to this layer through MCP tools (server.py). The layer talks to
the bank (bank.py) and applies the payment rules (gate.py). The agent never
sees tokens, consent ids or bank error codes.
"""
