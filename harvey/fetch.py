"""Old import path for the Apollo fetch, which now lives in ``sellbird.fetch``.

Kept so anything still written against the Harvey name (a scheduled cloud
routine's prompt, an old script) keeps working: ``import harvey.fetch`` and
``from harvey.fetch import X`` get the very same module object as
``sellbird.fetch``. Remove it together with the rest of ``harvey/``.
"""

import sys

from harvey.cli import _find_sellbird

_find_sellbird()
import sellbird.fetch as _fetch  # noqa: E402

sys.modules[__name__] = _fetch
