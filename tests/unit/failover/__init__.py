"""In-process end-to-end failover scenario tests (spec §8, Task 16).

Drives the real resolver -> registry -> dispatcher -> connector stack against
two in-process aiohttp stub servers on 127.0.0.1 ephemeral ports -- no
external service, so these tests live in the unit tree, not under
`pytest.mark.integration`.
"""

from __future__ import annotations
