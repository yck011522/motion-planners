from __future__ import print_function

import numpy as np
import argparse
import time
import random

from ..parabolic import opt_straight_line
from ..retime import min_linear_spline, spline_duration
from .discretize import time_discretize_curve, V_MAX, A_MAX
from .limits import get_max_velocity
from .smooth import smooth
from .samplers import get_sample_fn, get_collision_fn, get_extend_fn, get_distance_fn
from .viewer import create_box, draw_environment, add_points, \
    add_roadmap, get_box_center, add_path, create_cylinder
from ..utils import user_input, profiler, INF, compute_path_cost, get_distance, elapsed_time, get_pairs, remove_redundant, waypoints_from_path
from ..prm import prm
from ..lazy_prm import lazy_prm
from ..rrt_connect import rrt_connect, birrt
from ..rrt import rrt
from ..rrt_star import rrt_star
from ..smoothing import smooth_path
from ..lattice import lattice

ALGORITHMS = [
    prm,
    lazy_prm,
    rrt,
    rrt_connect,
    birrt,
    rrt_star,
    lattice,
    # TODO: https://ompl.kavrakilab.org/planners.html
]

##################################################

def retime_path(path, velocity=get_max_velocity(V_MAX), **kwargs):
    from scipy.interpolate import CubicHermiteSpline
    d = len(path[0])
    # v_max = 5.*np.ones(d)
    # a_max = v_max / 1.
    v_max, a_max = V_MAX, A_MAX

    waypoints = remove_redundant(path)
    waypoints = waypoints_from_path(waypoints)
    #durations = [0.] + [get_distance(*pair) / velocity for pair in get_pairs(waypoints)]
    durations = [0.] + [max(spline_duration(opt_straight_line(x1[k], x2[k], v_max=v_max[k], a_max=a_max[k])) for k in range(d))
                        for x1, x2 in get_pairs(waypoints)] # min_linear_spline | opt_straight_line
    #durations = [0.] + [solve_multivariate_ramp(x1, x2, np.zeros(d), np.zeros(d), v_max, a_max)
    #                     for x1, x2 in get_pairs(waypoints)]

    times = np.cumsum(durations)
    velocities = [np.zeros(len(waypoint)) for waypoint in waypoints]

    positions_curve = CubicHermiteSpline(times, waypoints, dydx=velocities)
    #positions_curve = interp1d(times, waypoints, kind='quadratic', axis=0) # Cannot differentiate

    positions_curve = smooth(positions_curve,
                             #v_max=None, a_max=None,
                             v_max=v_max, a_max=a_max,
                             **kwargs)
    return positions_curve

def interpolate_path(path, velocity=1., kind='linear', **kwargs): # linear | slinear | quadratic | cubic
    from scipy.interpolate import CubicHermiteSpline
    #from numpy import polyfit
    waypoints = remove_redundant(path)
    waypoints = waypoints_from_path(waypoints)

    #print(len(path), len(waypoints))
    differences = [0.] + [get_distance(*pair) / velocity for pair in get_pairs(waypoints)]
    times = np.cumsum(differences) / velocity
    #positions_curve = interp1d(times, waypoints, kind=kind, axis=0, **kwargs)
    #positions_curve = CubicSpline(times, waypoints, bc_type='clamped')
    velocities = [np.zeros(len(waypoint)) for waypoint in waypoints]
    positions_curve = CubicHermiteSpline(times, waypoints, dydx=velocities)
    #velocities_curve = positions_curve.derivative()
    #print([velocities_curve(t) for t in times])

    d = len(path[0])
    v_max = 5.*np.ones(d)
    a_max = v_max / 1.
    positions_curve = smooth(positions_curve, v_max, a_max)
    return positions_curve


##################################################

def main():
    """
    Creates and solves the 2D motion planning problem.
    """
    # https://github.com/caelan/pddlstream/blob/master/examples/motion/run.py
    # TODO: 3D work and CSpace
    # TODO: visualize just the tool frame of an end effector

    np.set_printoptions(precision=3)
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', '--algorithm', default='rrt_connect',
                        help='The algorithm to use.')
    parser.add_argument('-d', '--draw', action='store_true',
                        help='When enabled, draws the roadmap')
    parser.add_argument('-r', '--restarts', default=0, type=int,
                        help='The number of restarts.')
    parser.add_argument('-s', '--smooth', action='store_true',
                        help='When enabled, smooths paths.')
    parser.add_argument('-t', '--time', default=1., type=float,
                        help='The maximum runtime.')
    parser.add_argument('--seed', default=None, type=int,
                        help='The random seed to use.')
    args = parser.parse_args()
    print(args)

    seed = args.seed
    random.seed(seed)
    np.random.seed(seed)

    #########################

    obstacles = [
        create_box(center=(.35, .75), extents=(.25, .25)),
        create_box(center=(.75, .35), extents=(.25, .25)),
        #create_box(center=(.75, .35), extents=(.225, .225)),
        create_box(center=(.5, .5), extents=(.25, .25)),
        #create_box(center=(.5, .5), extents=(.225, .225)),
        create_cylinder(center=(.25, .25), radius=.1),
    ]

    # TODO: alternate sampling from a mix of regions
    regions = {
        'env': create_box(center=(.5, .5), extents=(1., 1.)),
        'green': create_box(center=(.8, .8), extents=(.1, .1)),
    }
    environment = regions['env']

    start = np.array([0., 0.])
    goal = 'green'
    if isinstance(goal, str) and (goal in regions):
        goal = get_box_center(regions[goal])
    else:
        goal = np.array([1., 1.])

    title = args.algorithm
    if args.smooth:
        title += '+shortcut'
    viewer = draw_environment(obstacles, regions, title=title)

    #########################

    #connected_test, roadmap = get_connected_test(obstacles)
    distance_fn = get_distance_fn(weights=[1, 1]) # distance_fn

    # samples = list(islice(region_gen('env'), 100))
    with profiler(field='cumtime'): # cumtime | tottime
        # TODO: cost bound & best cost
        for _ in range(args.restarts+1):
            start_time = time.time()
            collision_fn, cfree = get_collision_fn(environment, obstacles)
            sample_fn, samples = get_sample_fn(environment, obstacles=[], use_halton=False) # obstacles
            extend_fn, roadmap = get_extend_fn(environment, obstacles=obstacles)  # obstacles | []

            if args.algorithm == 'prm':
                path = prm(start, goal, distance_fn, sample_fn, extend_fn, collision_fn,
                           num_samples=200)
            elif args.algorithm == 'lazy_prm':
                path = lazy_prm(start, goal, sample_fn, extend_fn, collision_fn,
                                num_samples=200, max_time=args.time)[0]
            elif args.algorithm == 'rrt':
                path = rrt(start, goal, distance_fn, sample_fn, extend_fn, collision_fn,
                           iterations=INF, max_time=args.time)
            elif args.algorithm == 'rrt_connect':
                path = rrt_connect(start, goal, distance_fn, sample_fn, extend_fn, collision_fn,
                                   iterations=INF, max_time=args.time)
            elif args.algorithm == 'birrt':
                path = birrt(start, goal, distance_fn=distance_fn, sample_fn=sample_fn,
                             extend_fn=extend_fn, collision_fn=collision_fn,
                             restarts=2, iterations=INF, max_time=args.time, smooth=100)
            elif args.algorithm == 'rrt_star':
                path = rrt_star(start, goal, distance_fn, sample_fn, extend_fn, collision_fn,
                                radius=1, max_iterations=INF, max_time=args.time)
            elif args.algorithm == 'lattice':
                path = lattice(start, goal, extend_fn, collision_fn, distance_fn=distance_fn)
            else:
                raise NotImplementedError(args.algorithm)
            paths = [] if path is None else [path]

            #paths = random_restarts(rrt_connect, start, goal, distance_fn=distance_fn, sample_fn=sample_fn,
            #                         extend_fn=extend_fn, collision_fn=collision_fn, restarts=INF,
            #                         max_time=args.time, max_solutions=INF, smooth=100) #, smooth=1000, **kwargs)

            # paths = exhaustively_select_portfolio(paths, k=2)
            # print(score_portfolio(paths))

            #########################

            if args.draw:
                # roadmap = samples = cfree = []
                add_roadmap(viewer, roadmap, color='black')
                add_points(viewer, samples, color='red', radius=2)
                #add_points(viewer, cfree, color='blue', radius=2)

            print('Solutions ({}): {} | Time: {:.3f}'.format(len(paths), [(len(path), round(compute_path_cost(
                path, distance_fn), 3)) for path in paths], elapsed_time(start_time)))
            for path in paths:
                #path = path[:1] + path[-2:]
                path = waypoints_from_path(path)
                add_path(viewer, path, color='green')
                #curve = interpolate_path(path) # , collision_fn=collision_fn)
                curve = retime_path(path, collision_fn=collision_fn)
                _, path = time_discretize_curve(curve)
                #add_points(viewer, [curve(t) for t in curve.x])
                add_path(viewer, path, color='red')

            if args.smooth:
                for path in paths:
                    extend_fn, roadmap = get_extend_fn(obstacles=obstacles)  # obstacles | []
                    smoothed = smooth_path(path, extend_fn, collision_fn, iterations=INF, max_time=args.time)
                    print('Smoothed distance_fn: {:.3f}'.format(compute_path_cost(smoothed, distance_fn)))
                    add_path(viewer, smoothed, color='red')
            user_input('Finish?')

if __name__ == '__main__':
    main()
