"""Copyright (C) LogicRF Technologies (Chengdu) Co., Ltd. All Rights Reserved.

@file __init__.py
@brief H102 device-control API version and shared client package without Linux accounts.
@details Protocol 1 exposes fixed operations; IQ never travels through this package. Separate deployment tools handle
         board installation/maintenance permissions; ordinary connections never request SSH/sudo credentials.
@author LogicRF Technologies
@architect LogicRF
@email info@logicrf.com
@website https://www.logicrf.com
@version 0.1.0
@date 2026-10-17
"""

__version__ = '0.1.0'
PROTOCOL_VERSION = 1
DEFAULT_PORT = 30432

# End of file: __init__.py
# Contact: info@logicrf.com
# Website: https://www.logicrf.com
