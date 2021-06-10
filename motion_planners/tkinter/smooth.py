import random
import time

import numpy as np

from motion_planners.tkinter.limits import check_spline
from motion_planners.tkinter.discretize import time_discretize_curve
from motion_planners.parabolic import solve_multi_poly, MultiPPoly, solve_multivariate_ramp
from motion_planners.retime import trim, spline_duration
from motion_planners.utils import INF, elapsed_time, get_pairs, find

def find_lower_bound(x1, x2, v1=None, v2=None, v_max=None, a_max=None):
    d = len(x1)
    if v_max is None:
        v_max = np.full(d, INF)
    if a_max is None:
        a_max = np.full(d, INF)
    lower_bounds = [
        # Instantaneously accelerate
        np.linalg.norm(np.divide(np.subtract(x2, x1), v_max), ord=INF),
    ]
    if (v1 is not None) and (v2 is not None):
        lower_bounds.extend([
            np.linalg.norm(np.divide(np.subtract(v2, v1), a_max), ord=INF),
        ])
    return max(lower_bounds)

##################################################

def test_spline(best_t, x1, x2, v1, v2):
    observations = [
        (0., x1[0], 0),
        (best_t, x2[0], 0),
        (0., v1[0], 1),
        (best_t, v2[0], 1),
    ]
    degree = len(observations) - 1

    from numpy import poly1d, polyfit
    terms = []
    for k in range(degree + 1):
        coeffs = np.zeros(degree + 1)
        coeffs[k] = 1.
        terms.append(poly1d(coeffs))
    # series = poly1d(np.ones(degree+1))

    A = []
    b = []
    for t, v, nu in observations:
        A.append([term.deriv(m=nu)(t) for term in terms])
        b.append(v)
    print(A)
    print(b)
    print(np.linalg.solve(A, b))
    # print(polyfit([t for t, _, nu in observations if nu == 0],
    #              [v for _, v, nu in observations if nu == 0], deg=degree))
    # TODO: compare with CubicHermiteSpline


def get_curve_collision_fn(collision_fn=lambda q: False, max_velocities=None, max_accelerations=None): # a_max

    def curve_collision_fn(curve, t0=None, t1=None):
        if curve is None:
            return True
        if not check_spline(curve, v_max=max_velocities, a_max=None, verbose=False,
                            #start_t=t0, end_t=t1,
                            ):
            return True
        _, samples = time_discretize_curve(curve, verbose=False,
                                           start_t=t0, end_t=t1,
                                           #max_velocities=v_max,
                                           )
        if any(map(collision_fn, samples)):
           return True
    return curve_collision_fn


def smooth_curve(start_positions_curve, v_max, a_max, collision_fn=lambda q: False,
                 intermediate=True, cubic=True, refit=True, num=1000, min_improve=0., max_time=INF):
    # TODO: rename smoothing.py to shortcutting.py
    from scipy.interpolate import CubicHermiteSpline, CubicSpline
    start_time = time.time()
    curve_collision_fn = get_curve_collision_fn(collision_fn, max_velocities=v_max, max_accelerations=a_max)

    if curve_collision_fn(start_positions_curve, t0=None, t1=None):
        #return None
        return start_positions_curve
    positions_curve = start_positions_curve
    for iteration in range(num):
        if elapsed_time(start_time) >= max_time:
            break
        times = positions_curve.x
        durations = [0.] + [t2 - t1 for t1, t2 in get_pairs(times)] # includes start
        positions = [positions_curve(t) for t in times]
        velocities_curve = positions_curve.derivative()
        velocities = [velocities_curve(t) for t in times]

        # ts = [times[0], times[-1]]
        # t1, t2 = positions_curve.x[0], positions_curve.x[-1]
        t1, t2 = np.random.uniform(times[0], times[-1], 2) # TODO: sample based on position
        if t1 > t2: # TODO: minimum distance from a knot
            t1, t2 = t2, t1

        ts = [t1, t2]
        i1 = find(lambda i: times[i] <= t1, reversed(range(len(times)))) # index before t1
        i2 = find(lambda i: times[i] >= t2, range(len(times))) # index after t2
        assert i1 != i2

        spliced_positions = [positions_curve(t) for t in ts]
        spliced_velocities = [velocities_curve(t) for t in ts]
        #assert all(abs(v) <= v_max for v in spliced_velocities)
        #if any(np.greater(np.absolute(v), v_max).any() for v in spliced_velocities):
        #    continue # TODO: do the same with collisions
        x1, x2 = spliced_positions
        v1, v2 = spliced_velocities

        #min_t = 0
        min_t = find_lower_bound(x1, x2, v1, v2, v_max=v_max, a_max=a_max)
        #min_t = optimistic_time(x1, x2, v_max=v_max, a_max=a_max)
        max_t = (t2 - t1) - min_improve
        if min_t >= max_t: # TODO: also limit the distance/duration between these two points
            continue

        #best_t = random.uniform(min_t, max_t)
        best_t = solve_multivariate_ramp(x1, x2, v1, v2, v_max, a_max)
        #best_t = min_t
        if (best_t is None) or (best_t >= max_t):
            continue
        #best_t += 1e-3
        #print(min_t, best_t, max_t)
        spliced_durations = [t1 - times[i1], best_t, times[i2] - t2]
        spliced_times = [0, best_t]
        #spliced_times = [t1, (t1 + best_t)]

        # new_positions_curve = CubicHermiteSpline(spliced_times, spliced_positions, dydx=spliced_velocities)
        # # print(new_positions_curve.x, new_positions_curve.c)
        # if not check_spline(new_positions_curve, v_max, a_max):
        #     continue

        new_positions_curve = solve_multi_poly(times=spliced_times, positions=spliced_positions, velocities=spliced_velocities,
                                               v_max=v_max, a_max=a_max)
        if (new_positions_curve is None) or (spline_duration(new_positions_curve) > max_t):
            continue
        if curve_collision_fn(new_positions_curve, t0=None, t1=None):
            continue
        #print(new_positions_curve.hermite_spline().c[0,...])

        if intermediate:
            spliced_positions = [new_positions_curve(x) for x in new_positions_curve.x]
            spliced_velocities = [new_positions_curve(x, nu=1) for x in new_positions_curve.x]
            spliced_durations = [t1 - times[i1]] + [x - new_positions_curve.x[0]
                                                    for x in new_positions_curve.x[1:]] + [times[i2] - t2]

        new_durations = np.concatenate([
            durations[:i1+1], spliced_durations, durations[i2+1:]])
        #assert len(new_durations) == (i1 + 1) + (len(durations) - i2) + 2
        new_times = np.cumsum(new_durations)
        #new_times = [new_times[0]] + [t2 for t1, t2 in get_pairs(new_times) if t2 > t1]
        new_positions = positions[:i1+1] + spliced_positions + positions[i2:]
        new_velocities = velocities[:i1+1] + spliced_velocities + velocities[i2:]
        #if not all(np.less_equal(np.absolute(v), v_max).all() for v in new_velocities):
        #    continue

        if refit:
            if cubic:
                # new_positions_curve = CubicSpline(new_times, new_positions)
                new_positions_curve = CubicHermiteSpline(new_times, new_positions, dydx=new_velocities)
            else:
                new_positions_curve = solve_multi_poly(new_times, new_positions, new_velocities, v_max, a_max)
            # new_t1 = new_times[i1+1]
            # new_t2 = new_times[i1+2]
            # new_t2 = new_times[-(len(times) - i2 + 1)]
            # new_velocities_curve = new_positions_curve.derivative()
            # print(v2, new_velocities_curve(new_t2))
        else:
            pre_curve = trim(positions_curve, end=t1)
            post_curve = trim(positions_curve, start=t2)
            new_positions_curve = pre_curve.append(new_positions_curve, post_curve)
            print(spliced_positions)
            print(spliced_velocities)
            print(new_positions_curve.x[0], new_positions_curve.x[-1])
            print(new_positions_curve(t1), new_positions_curve(t1 + best_t))
            print(new_positions_curve(t1, 1), new_positions_curve(t1 + best_t, 1)) # TODO: test all knots
            #input()
        if (new_positions_curve is None) or (spline_duration(new_positions_curve) >= spline_duration(positions_curve)):
            continue
        #print(new_positions_curve.c[0,...])
        print('Iterations: {} | Current time: {:.3f} | New time: {:.3f} | Elapsed time: {:.3f}'.format(
            iteration, spline_duration(positions_curve), spline_duration(new_positions_curve), elapsed_time(start_time)))

        if curve_collision_fn(new_positions_curve, t0=None, t1=None):
            continue
        positions_curve = new_positions_curve
    print('Iterations: {} | Start time: {:.3f} | End time: {:.3f} | Elapsed time: {:.3f}'.format(
        num, start_positions_curve.x[-1], positions_curve.x[-1], elapsed_time(start_time)))
    check_spline(positions_curve, v_max, a_max)
    return positions_curve
