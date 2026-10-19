"""Sandbox execution layer (PRD-079).

Client of the gateway: talks to the local proxy over HTTP only, never
imports gateway internals (routing / executor / translate / resilience /
failover).
"""
