import numpy as np
import trimesh

from ubanl.geometry import (
    cut_with_plane,
    farthest_points_in_region,
    obb_fits,
    placeable_region,
    section_at_plane,
)


def test_cut_conserves_volume(sphere_100):
    neg, pos = cut_with_plane(sphere_100, origin=[0, 0, 50.0], normal=[0, 0, 1])
    assert len(neg) == 1 and len(pos) == 1
    for part in (*neg, *pos):
        assert part.is_watertight and part.is_volume
    total = sum(p.volume for p in (*neg, *pos))
    assert abs(total - sphere_100.volume) / sphere_100.volume < 1e-3


def test_cut_missing_plane_returns_one_side(sphere_100):
    neg, pos = cut_with_plane(sphere_100, origin=[0, 0, 500.0], normal=[0, 0, 1])
    assert len(pos) == 0 and len(neg) == 1


def test_section_polygons(sphere_100):
    section = section_at_plane(sphere_100, origin=[0, 0, 50.0], normal=[0, 0, 1])
    assert section is not None
    # equator cross-section of a 50 mm sphere
    assert abs(section.area - np.pi * 50.0**2) / (np.pi * 50.0**2) < 0.02
    center_world = section.point_to_world(np.zeros(2))
    assert abs(center_world[2] - 50.0) < 1e-6


def test_obb_fits_rotated_box():
    box = trimesh.creation.box(extents=[100, 20, 20])
    box.apply_transform(trimesh.transformations.random_rotation_matrix())
    assert obb_fits(box, np.sort(np.array([25.0, 25.0, 105.0])))
    assert not obb_fits(box, np.sort(np.array([25.0, 25.0, 95.0])))


def test_farthest_points_spread(sphere_100):
    section = section_at_plane(sphere_100, origin=[0, 0, 50.0], normal=[0, 0, 1])
    region = placeable_region(section.polygons, inset=8.0)
    rng = np.random.default_rng(0)
    points = farthest_points_in_region(region, 3, grid_step=4.0, rng=rng)
    assert len(points) == 3
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            assert np.linalg.norm(points[i] - points[j]) > 15.0
