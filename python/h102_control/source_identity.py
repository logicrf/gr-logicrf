"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file source_identity.py
@brief Separate translated host source identity from installed board-helper requirements.
@details Ordinary API sessions request helpers already installed on the board; they
         never upload translated copies. Map only reviewed exact local hashes to
         their corresponding installed hashes. Unknown or edited sources remain
         unchanged and face the existing independent server identity checks.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 1.2.2rc2
@date 2026-10-20
"""
import json
from pathlib import Path


def installed_sources(local_sources):
    """Return installed-helper requirements for exact reviewed local hashes.

    Preserve unknown and original installed hashes. Never add helper names, alter
    device allowlists or classify edited files as reviewed. The device verifies
    its own installed bytes independently and returns their actual hashes.
    """
    pins = json.loads(Path(__file__).with_name('installed-helpers.json').read_text())
    if pins.get('schema') != 1:
        raise RuntimeError('CONTROL_HELPER_IDENTITY_SCHEMA')
    result = {}
    for name, digest in local_sources.items():
        entry = pins['helpers'].get(name)
        result[name] = (entry['installed_sha256']
                        if entry and digest == entry['local_sha256'] else digest)
    return result

# End of file: source_identity.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
