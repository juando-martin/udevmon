udevmon documentation
=====================

Run your own commands when USB devices or removable filesystems appear or
disappear: a user-level daemon, no root, configured in one TOML file.

This manual is the reference for people who integrate with udevmon or work
on it. The user guide is ``README.md`` and the two man pages. The manual is
split into getting started, integration contracts, the interface policy,
internal reference and the CLI reference. Building it never reads your
configuration, starts the daemon, talks to a running one or touches a
device.

.. toctree::
   :maxdepth: 2

   getting-started
   integration
   api
   internals
   modules
   cli
   contributing
