Implementation reference
========================

The core modules, for maintainers. None of this is a public API
(:doc:`api`). ``SPECIFICATIONS.md`` is the authoritative design; the module
summaries below come from the source docstrings.

Design records and manuals
--------------------------

* :download:`Specifications <../SPECIFICATIONS.md>`
* :download:`README (user guide) <../README.md>`
* :download:`Development guide <../DEVELOPMENT.md>`
* :download:`Changelog <../CHANGELOG.md>`
* :download:`Command man page <../man/udevmon.1>`
* :download:`Configuration man page <../man/udevmon.toml.5>`

Data model
----------

.. automodule:: udevmon.events
   :members:

.. automodule:: udevmon.matching
   :members:

Configuration
-------------

.. automodule:: udevmon.config
   :members:

.. automodule:: udevmon.configedit
   :members:

Daemon
------

.. automodule:: udevmon.daemon
   :members:

.. automodule:: udevmon.engine
   :members:

.. automodule:: udevmon.actions
   :members:

.. automodule:: udevmon.control
   :members:

Event sources
-------------

.. automodule:: udevmon.sources

.. automodule:: udevmon.sources.udev
   :members:

.. automodule:: udevmon.sources.udisks
   :members:
