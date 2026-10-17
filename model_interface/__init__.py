"""model-interface: a model gateway that taps the wire.

Configure providers, create agents, author rules. An agent points its SDK at one of two doors with its
virtual key and names its model as provider/model. The gateway passes the call through verbatim, reads
every line of the response into a record by shape as it streams, runs the rules in that loop, and can
stop any of it at the gateway, an agent or a session.
"""
