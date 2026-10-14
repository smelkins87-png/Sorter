"""On/off switch for the second photo after each drop.

This plugin does nothing itself: it exists so the check can be switched on
and off from the touchscreen (System settings > Available plugins), which
creates or removes disabled.txt in this folder. The work is done by the
always-loaded "localsorter" plugin, which looks at that file before every
card, so the switch takes effect immediately, with no restart.
"""
from lib.utils.pluginbase import PluginBase


class Verifydrop(PluginBase):
    @staticmethod
    def get_version():
        return "1.1.0"
