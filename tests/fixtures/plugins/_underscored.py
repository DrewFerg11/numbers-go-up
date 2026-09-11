"""Underscore-prefixed fixture: discovery must skip this without even
attempting to import it. Deliberately invalid syntax proves that -- if
discovery ever tried to import this file, the import itself would raise.
"""

this is not valid python syntax !!!
