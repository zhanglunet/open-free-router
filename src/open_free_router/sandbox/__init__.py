"""PRD-079: optional code-execution sandbox layer above the gateway.

T2 scope: execution engine (runner), backend providers, standalone config.

The sandbox is a *client* of the gateway (HTTP + proxy.token); it never
imports gateway internals (routing / executor / translate / resilience /
failover). T3 adds the model caller + CLI on top of this package.
"""
