"""Local, self-contained card recognition for the Magic Sorter.

Replaces the vendor cloud API (api2..api5.magic-sorter.com) with a server that
runs on the sorter's Raspberry Pi (or any PC on the LAN). Card data and prices
come from Scryfall's public bulk files; no scan ever leaves the machine.
"""

__version__ = "0.1.0"
