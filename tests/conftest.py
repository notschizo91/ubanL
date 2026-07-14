import numpy as np
import pytest
import trimesh


@pytest.fixture(scope="session")
def sphere_100() -> trimesh.Trimesh:
    """100 mm diameter sphere sitting on z=0."""
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=50.0)
    mesh.apply_translation([0, 0, 50.0])
    return mesh


@pytest.fixture(scope="session")
def capsule_tall() -> trimesh.Trimesh:
    """A tall capsule: 60 mm radius, ~400 mm tall, base at z=0."""
    mesh = trimesh.creation.capsule(radius=60.0, height=280.0, count=[24, 24])
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return mesh
