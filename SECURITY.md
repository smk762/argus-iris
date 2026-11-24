# Security

Report a vulnerability privately through GitHub's "Report a vulnerability" on this repository's Security tab.

## Trust model

iris is for a trusted private network. Do not expose either port to the internet.

**The agent (:9110) is root-equivalent on its host.** It has the Docker socket and the host PID namespace. Reads
(`/snapshot`, `/metrics`, `/health`) need no token and list the host's containers. Container start, stop and restart
need `IRIS_AGENT_TOKEN` and are off while it is unset. They apply only to containers in the agent's own
`IRIS_AGENT_CONTAINERS`, or, where the host opts in with `IRIS_AGENT_MANAGE_LABEL`, to containers labelled
`argus.iris.manage=true`. Neither the broker's config nor a registered manifest can widen that.

**The broker (:8120).** Reads are open, except the peer lookup. Lease and tenant routes need `ARGUS_GPU_BROKER_TOKEN`
only when it is set, so set it. Warm, evict, peer lookup and forgetting a node are refused while it is unset. Browser
writes from other origins are refused.

**Two tokens.** Callers' and agents' tokens are separate, so a caller cannot act on containers directly. Announcing a
node needs the agent token.

**Registered services.** The broker never contacts a host a caller named: a service's address comes from the agent's
inventory. A registered tenant's admin token goes only to that tenant's services and is stored in
`$IRIS_STATE_DIR/registrations.json` with mode 0600.

**Ports bind to localhost by default.** In compose, `HOST_IP` overrides it (a private address; the broker needs to
reach the agents). Outside Docker, use `IRIS_HOST` and `IRIS_AGENT_HOST`, or `--host`. The image listens on all
interfaces inside its container, so `docker run -p` needs an address too.
