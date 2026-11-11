"""
The SQLAlchemy ``Base`` every deep-asteroids table is declared on.

This is find-asteroids' own ``Base``, not a new one: importing
``find_asteroids.models`` registers its ``search``/``result``/``tracklet``/
``gathered``/``points`` tables on the same metadata, so
``Base.metadata.create_all()`` creates them alongside ours and our tables
can declare real foreign keys to them. Import any module that declares
deep-asteroids tables here, so ``create_all()`` sees every table.
"""
from find_asteroids.models import Base  # noqa: F401 -- re-exported

import find_asteroids.models  # noqa: F401 -- registers find-asteroids' tables on Base.metadata
