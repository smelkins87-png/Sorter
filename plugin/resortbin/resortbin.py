"""On/off switch for re-sorting the manual-check bin after a review.

This plugin does nothing itself: it exists so the feature can be switched on
and off from the touchscreen (System settings > Available plugins), which
creates or removes disabled.txt in this folder. The work is done by the
always-loaded "localsorter" plugin.
"""
from lib.utils.pluginbase import PluginBase


class Resortbin(PluginBase):
    @staticmethod
    def get_version():
        return "1.0.0"
