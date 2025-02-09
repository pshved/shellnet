import math
import random
import itertools
import numpy as np
import tensorflow as tf
from numpy import linalg as LA
from transforms3d.euler import euler2mat

# the returned indices will be used by tf.gather_nd
def get_indices(batch_size, sample_num, point_num, pool_setting=None):
    if not isinstance(point_num, np.ndarray):
        point_nums = np.full((batch_size), point_num)
    else:
        point_nums = point_num

    indices = []
    for i in range(batch_size):
        pt_num = point_nums[i]
        if pool_setting is None:
            pool_size = pt_num
        else:
            if isinstance(pool_setting, int):
                pool_size = min(pool_setting, pt_num)
            elif isinstance(pool_setting, tuple):
                pool_size = min(random.randrange(pool_setting[0], pool_setting[1]+1), pt_num)
        if pool_size > sample_num:
            choices = np.random.choice(pool_size, sample_num, replace=False)
        else:
            choices = np.concatenate((np.random.choice(pool_size, pool_size, replace=False),
                                      np.random.choice(pool_size, sample_num - pool_size, replace=True)))
        if pool_size < pt_num:
            choices_pool = np.random.choice(pt_num, pool_size, replace=False)
            choices = choices_pool[choices]
        choices = np.expand_dims(choices, axis=1)
        choices_2d = np.concatenate((np.full_like(choices, i), choices), axis=1)
        indices.append(choices_2d)
    return np.stack(indices)

def gauss_clip(mu, sigma, clip):
    v = random.gauss(mu, sigma)
    v = max(min(v, mu + clip * sigma), mu - clip * sigma)
    return v

def uniform(bound):
    return bound * (2 * random.random() - 1)


def scaling_factor(scaling_param, method):
    try:
        scaling_list = list(scaling_param)
        return random.choice(scaling_list)
    except:
        if method == 'g':
            return gauss_clip(1.0, scaling_param, 3)
        elif method == 'u':
            return 1.0 + uniform(scaling_param)


def rotation_angle(rotation_param, method):
    try:
        rotation_list = list(rotation_param)
        return random.choice(rotation_list)
    except:
        if method == 'g':
            return gauss_clip(0.0, rotation_param, 3)
        elif method == 'u':
            return uniform(rotation_param)


def get_xforms(xform_num, rotation_range=(0, 0, 0, 'u'), scaling_range=(0.0, 0.0, 0.0, 'u'), order='rxyz'):
    xforms = np.empty(shape=(xform_num, 3, 3))
    rotations = np.empty(shape=(xform_num, 3, 3))
    for i in range(xform_num):
        rx = rotation_angle(rotation_range[0], rotation_range[3])
        ry = rotation_angle(rotation_range[1], rotation_range[3])
        rz = rotation_angle(rotation_range[2], rotation_range[3])
        rotation = euler2mat(rx, ry, rz, order)

        sx = scaling_factor(scaling_range[0], scaling_range[3])
        sy = scaling_factor(scaling_range[1], scaling_range[3])
        sz = scaling_factor(scaling_range[2], scaling_range[3])
        scaling = np.diag([sx, sy, sz])

        xforms[i, :] = scaling * rotation
        rotations[i, :] = rotation
    return xforms, rotations



def augment(points, xforms, range=None):
    points_xformed = tf.matmul(points, xforms, name='points_xformed')
    if range is None:
        return points_xformed

    jitter_data = range * tf.random_normal(tf.shape(points_xformed), name='jitter_data')
    jitter_clipped = tf.clip_by_value(jitter_data, -5 * range, 5 * range, name='jitter_clipped')
    return points_xformed + jitter_clipped


# A shape is (N, C)
def distance_matrix(A):
    r = tf.reduce_sum(A * A, 1, keepdims=True)
    m = tf.matmul(A, tf.transpose(A))
    D = r - 2 * m + tf.transpose(r)
    return D


# A shape is (N, P, C)
def batch_distance_matrix(A):
    r = tf.reduce_sum(A * A, axis=2, keepdims=True)
    m = tf.matmul(A, tf.transpose(A, perm=(0, 2, 1)))
    D = r - 2 * m + tf.transpose(r, perm=(0, 2, 1))
    return D


# A shape is (N, P_A, C), B shape is (N, P_B, C)
# D shape is (N, P_A, P_B)
def batch_distance_matrix_general(A, B):
    r_A = tf.reduce_sum(A * A, axis=2, keepdims=True)
    r_B = tf.reduce_sum(B * B, axis=2, keepdims=True)
    m = tf.matmul(A, tf.transpose(B, perm=(0, 2, 1)))
    D = r_A - 2 * m + tf.transpose(r_B, perm=(0, 2, 1))
    return D

# A shape is (N, P_A, C), B shape is (N, P_B, C)
# D shape is (N, P_A, P_B)
def batch_distance_matrix_general_l1(A, B):
    """L1 norm"""
    # Add one dimension
    Ac = tf.transpose(tf.expand_dims(A, axis=-1), perm=(0,2,1,3))  # (N, C, P_A, 1)
    Bc = tf.transpose(tf.expand_dims(B, axis=-1), perm=(0,2,3,1))  # (N, C, 1, P_B)
    A_1s = tf.ones_like(Ac)
    B_1s = tf.ones_like(Bc)

    A_exp = tf.matmul(Ac, B_1s)  # (N, C, P_A, P_B) where each A[ijk] value is copied along x in A_exp[ijkx]
    B_exp = tf.matmul(A_1s, Bc)  # (N, C, P_A, P_B) where each B[ijk] value is copied along x in B_exp[ijxk]
    # A_exp - B_exp is now pairwise a_i - b_j for each channel :-)

    D = tf.reduce_max(tf.abs(A_exp - B_exp), axis=1)

    return D


# A shape is (N, P, C)
def find_duplicate_columns(A):
    N = A.shape[0]
    P = A.shape[1]
    indices_duplicated = np.fill((N, 1, P), 1, dtype=np.int32)
    for idx in range(N):
        _, indices = np.unique(A[idx], return_index=True, axis=0)
        indices_duplicated[idx, :, indices] = 0
    return indices_duplicated


# add a big value to duplicate columns
def prepare_for_unique_top_k(D, A):
    indices_duplicated = tf.py_function(find_duplicate_columns, [A], tf.int32)
    D += tf.reduce_max(D)*tf.cast(indices_duplicated, tf.float32)


# return shape is (N, P, K, 2)
def knn_indices(points, k, sort=True, unique=True):
    points_shape = tf.shape(points)
    batch_size = points_shape[0]
    point_num = points_shape[1]

    D = batch_distance_matrix(points)
    if unique:
        prepare_for_unique_top_k(D, points)
    distances, point_indices = tf.nn.top_k(-D, k=k, sorted=sort)
    batch_indices = tf.tile(tf.reshape(tf.range(batch_size), (-1, 1, 1, 1)), (1, point_num, k, 1))
    indices = tf.concat([batch_indices, tf.expand_dims(point_indices, axis=3)], axis=3)
    return -distances, indices


# return shape is (N, P, K, 2)
def knn_indices_general(queries, points, k, sort=True, unique=True, norm='l2'):
    queries_shape = tf.shape(queries)
    batch_size = queries_shape[0]
    point_num = queries_shape[1]
    tmp_k = 0
    if norm == 'l1':
        D = batch_distance_matrix_general_l1(queries, points)
    else:
        D = batch_distance_matrix_general(queries, points)
    if unique:
        prepare_for_unique_top_k(D, points)
    _, point_indices = tf.nn.top_k(-D, k=k+tmp_k, sorted=sort)  # (N, P, K)
    # point_indices = tf.contrib.framework.argsort(D)
    # point_indices = point_indices[:,:,:k]
    batch_indices = tf.tile(tf.reshape(tf.range(batch_size), (-1, 1, 1, 1)), (1, point_num, k, 1))
    indices = tf.concat([batch_indices, tf.expand_dims(point_indices[:,:,tmp_k:], axis=3)], axis=3)
    return indices


# indices is (N, P, K, 2)
# return shape is (N, P, K, 2)
def sort_points(points, indices, sorting_method):
    indices_shape = tf.shape(indices)
    batch_size = indices_shape[0]
    point_num = indices_shape[1]
    k = indices_shape[2]

    nn_pts = tf.gather_nd(points, indices)  # (N, P, K, 3)
    if sorting_method.startswith('c'):
        if ''.join(sorted(sorting_method[1:])) != 'xyz':
            print('Unknown sorting method!')
            exit()
        epsilon = 1e-8
        nn_pts_min = tf.reduce_min(nn_pts, axis=2, keepdims=True)
        nn_pts_max = tf.reduce_max(nn_pts, axis=2, keepdims=True)
        nn_pts_normalized = (nn_pts - nn_pts_min) / (nn_pts_max - nn_pts_min + epsilon)  # (N, P, K, 3)
        scaling_factors = [math.pow(100.0, 3 - sorting_method.find('x')),
                           math.pow(100.0, 3 - sorting_method.find('y')),
                           math.pow(100.0, 3 - sorting_method.find('z'))]
        scaling = tf.constant(scaling_factors, shape=(1, 1, 1, 3))
        sorting_data = tf.reduce_sum(nn_pts_normalized * scaling, axis=-1)  # (N, P, K)
        sorting_data = tf.concat([tf.zeros((batch_size, point_num, 1)), sorting_data[:, :, 1:]], axis=-1)
    elif sorting_method == 'l2':
        nn_pts_center = tf.reduce_mean(nn_pts, axis=2, keepdims=True)  # (N, P, 1, 3)
        nn_pts_local = tf.subtract(nn_pts, nn_pts_center)  # (N, P, K, 3)
        sorting_data = tf.norm(nn_pts_local, axis=-1)  # (N, P, K)
    else:
        print('Unknown sorting method!')
        exit()
    _, k_indices = tf.nn.top_k(sorting_data, k=k, sorted=True)  # (N, P, K)
    batch_indices = tf.tile(tf.reshape(tf.range(batch_size), (-1, 1, 1, 1)), (1, point_num, k, 1))
    point_indices = tf.tile(tf.reshape(tf.range(point_num), (1, -1, 1, 1)), (batch_size, 1, k, 1))
    k_indices_4d = tf.expand_dims(k_indices, axis=3)
    sorting_indices = tf.concat([batch_indices, point_indices, k_indices_4d], axis=3)  # (N, P, K, 3)
    return tf.gather_nd(indices, sorting_indices)


# a b c
# d e f
# g h i
# a(ei − fh) − b(di − fg) + c(dh − eg)
def compute_determinant(A):
    return A[..., 0, 0] * (A[..., 1, 1] * A[..., 2, 2] - A[..., 1, 2] * A[..., 2, 1]) \
           - A[..., 0, 1] * (A[..., 1, 0] * A[..., 2, 2] - A[..., 1, 2] * A[..., 2, 0]) \
           + A[..., 0, 2] * (A[..., 1, 0] * A[..., 2, 1] - A[..., 1, 1] * A[..., 2, 0])


# A shape is (N, P, 3, 3)
# return shape is (N, P, 3)
def compute_eigenvals(A):
    A_11 = A[:, :, 0, 0]  # (N, P)
    A_12 = A[:, :, 0, 1]
    A_13 = A[:, :, 0, 2]
    A_22 = A[:, :, 1, 1]
    A_23 = A[:, :, 1, 2]
    A_33 = A[:, :, 2, 2]
    I = tf.eye(3)
    p1 = tf.square(A_12) + tf.square(A_13) + tf.square(A_23)  # (N, P)
    q = tf.trace(A) / 3  # (N, P)
    p2 = tf.square(A_11 - q) + tf.square(A_22 - q) + tf.square(A_33 - q) + 2 * p1  # (N, P)
    p = tf.sqrt(p2 / 6) + 1e-8  # (N, P)
    N = tf.shape(A)[0]
    q_4d = tf.reshape(q, (N, -1, 1, 1))  # (N, P, 1, 1)
    p_4d = tf.reshape(p, (N, -1, 1, 1))
    B = (1 / p_4d) * (A - q_4d * I)  # (N, P, 3, 3)
    r = tf.clip_by_value(compute_determinant(B) / 2, -1, 1)  # (N, P)
    phi = tf.acos(r) / 3  # (N, P)
    eig1 = q + 2 * p * tf.cos(phi)  # (N, P)
    eig3 = q + 2 * p * tf.cos(phi + (2 * math.pi / 3))
    eig2 = 3 * q - eig1 - eig3
    return tf.abs(tf.stack([eig1, eig2, eig3], axis=2))  # (N, P, 3)

# A shape is (N, P, 3, 3)
# return shape is (N, P, 3,3)
def compute_eigenvectors(A):
    # https://math.stackexchange.com/questions/2962480/general-form-for-eigenvector-of-a-3-by-3-symmetric-matrix
    N = tf.shape(A)[0]
    P = tf.shape(A)[1]
    eigvals = compute_eigenvals(A) 
    
    tmp = tf.eye(3,batch_shape=(N,P))

    d0 = tf.reshape(eigvals[:,:,0],[N,P,1,1]) 
    d1 = tf.reshape(eigvals[:,:,1],[N,P,1,1]) 
    d2 = tf.reshape(eigvals[:,:,2],[N,P,1,1]) 

    m0 = tf.multiply(d0,tmp)
    m1 = tf.multiply(d1,tmp)
    m2 = tf.multiply(d2,tmp)

    X = tf.random_uniform([N, P, 3, 1], minval=0, maxval=10,dtype=tf.float32)
    
    v0 = tf.matmul((A-m1), (A-m2))
    v0 = tf.matmul(v0,X)

    v1 = tf.matmul((A-m0), (A-m2))
    v1 = tf.matmul(v1,X)

    v2 = tf.matmul((A-m0), (A-m1))
    v2 = tf.matmul(v2,X)


    U = tf.concat([v0, v1, v2], axis=-1)
    U = tf.divide(U, tf.norm(U, axis=-2, keepdims = True))
    U = tf.transpose(U,perm=[0,1,3,2])  # to make sure that each row is a eigenvector

    return U

def compute_axis_z(A):
    # https://math.stackexchange.com/questions/2962480/general-form-for-eigenvector-of-a-3-by-3-symmetric-matrix
    N = tf.shape(A)[0]
    P = tf.shape(A)[1]
    eigvals = compute_eigenvals(A)   # the smallest is the last one
    
    tmp = tf.eye(3,batch_shape=(N,P))

    d0 = tf.reshape(eigvals[:,:,0],[N,P,1,1]) 
    d1 = tf.reshape(eigvals[:,:,1],[N,P,1,1]) 

    m0 = tf.multiply(d0,tmp)
    m1 = tf.multiply(d1,tmp)

    X = tf.random_uniform([N, P, 3, 1], minval=0, maxval=10,dtype=tf.float32)

    v2 = tf.matmul((A-m0), (A-m1))
    v2 = tf.matmul(v2,X)

    axis_z = tf.divide(v2, tf.norm(v2, axis=-2, keepdims = True))
    # U = tf.transpose(U,perm=[0,1,3,2])  # to make sure that each row is a eigenvector

    return axis_z

# P shape is (N, P, 3), N shape is (N, P, K, 3)
# return shape is (N, P)
def compute_curvature(nn_pts):
    nn_pts_mean = tf.reduce_mean(nn_pts, axis=2, keepdims=True)  # (N, P, 1, 3)
    nn_pts_demean = nn_pts - nn_pts_mean  # (N, P, K, 3)
    nn_pts_NPK31 = tf.expand_dims(nn_pts_demean, axis=-1)
    covariance_matrix = tf.matmul(nn_pts_NPK31, nn_pts_NPK31, transpose_b=True)  # (N, P, K, 3, 3)
    covariance_matrix_mean = tf.reduce_mean(covariance_matrix, axis=2)  # (N, P, 3, 3)
    eigvals = compute_eigenvals(covariance_matrix_mean)  # (N, P, 3)
    curvature = tf.reduce_min(eigvals, axis=-1) / (tf.reduce_sum(eigvals, axis=-1) + 1e-8)
    return curvature


def curvature_based_sample(nn_pts, k):
    curvature = compute_curvature(nn_pts)
    _, point_indices = tf.nn.top_k(curvature, k=k, sorted=False)

    pts_shape = tf.shape(nn_pts)
    batch_size = pts_shape[0]
    batch_indices = tf.tile(tf.reshape(tf.range(batch_size), (-1, 1, 1)), (1, k, 1))
    indices = tf.concat([batch_indices, tf.expand_dims(point_indices, axis=2)], axis=2)
    return indices


def random_choice_2d(size, prob_matrix):
    n_row = prob_matrix.shape[0]
    n_col = prob_matrix.shape[1]
    choices = np.ones((n_row, size), dtype=np.int32)
    for idx_row in range(n_row):
        choices[idx_row] = np.random.choice(n_col, size=size, replace=False, p=prob_matrix[idx_row])
    return choices


def inverse_density_sampling(points, k, sample_num):
    D = batch_distance_matrix(points)
    distances, _ = tf.nn.top_k(-D, k=k, sorted=False)
    distances_avg = tf.abs(tf.reduce_mean(distances, axis=-1)) + 1e-8
    prob_matrix = distances_avg / tf.reduce_sum(distances_avg, axis=-1, keepdims=True)
    point_indices = tf.py_function(random_choice_2d, [sample_num, prob_matrix], tf.int32)
    point_indices.set_shape([points.get_shape()[0], sample_num])

    batch_size = tf.shape(points)[0]
    batch_indices = tf.tile(tf.reshape(tf.range(batch_size), (-1, 1, 1)), (1, sample_num, 1))
    indices = tf.concat([batch_indices, tf.expand_dims(point_indices, axis=2)], axis=2)
    return indices


def batch_normalization(data, is_training, name, reuse=None):
    return tf.layers.batch_normalization(data, momentum=0.99, training=is_training,
                                         beta_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                                         gamma_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                                         reuse=reuse, name=name)


def separable_conv2d(input, output, name, is_training, kernel_size, depth_multiplier=1,
                     reuse=None, with_bn=True, activation=tf.nn.elu):
    conv2d = tf.layers.separable_conv2d(input, output, kernel_size=kernel_size, strides=(1, 1), padding='VALID',
                                        activation=activation,
                                        depth_multiplier=depth_multiplier,
                                        depthwise_initializer=tf.glorot_normal_initializer(),
                                        pointwise_initializer=tf.glorot_normal_initializer(),
                                        depthwise_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                                        pointwise_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                                        reuse=reuse, name=name, use_bias=not with_bn)
    return batch_normalization(conv2d, is_training, name + '_bn', reuse) if with_bn else conv2d


def depthwise_conv2d(input, depth_multiplier, name, is_training, kernel_size,
                     reuse=None, with_bn=True, activation=tf.nn.elu):
    conv2d = tf.contrib.layers.separable_conv2d(input, num_outputs=None, kernel_size=kernel_size, padding='VALID',
                                                activation_fn=activation,
                                                depth_multiplier=depth_multiplier,
                                                weights_initializer=tf.glorot_normal_initializer(),
                                                weights_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                                                biases_initializer=None if with_bn else tf.zeros_initializer(),
                                                biases_regularizer=None if with_bn else tf.contrib.layers.l2_regularizer(
                                                    scale=1.0),
                                                reuse=reuse, scope=name)
    return batch_normalization(conv2d, is_training, name + '_bn', reuse) if with_bn else conv2d


def conv2d(input, output, name, is_training, kernel_size,
           reuse=None, with_bn=True, activation=tf.nn.elu):
    conv2d = tf.layers.conv2d(input, output, kernel_size=kernel_size, strides=(1, 1), padding='VALID',
                              activation=activation,
                              kernel_initializer=tf.glorot_normal_initializer(),
                              kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                              reuse=reuse, name=name, use_bias=not with_bn)
    return batch_normalization(conv2d, is_training, name + '_bn', reuse) if with_bn else conv2d


def dense(input, output, name, is_training, reuse=None, with_bn=True, activation=tf.nn.relu):
    dense = tf.layers.dense(input, units=output, activation=activation,
                            kernel_initializer=tf.glorot_normal_initializer(),
                            kernel_regularizer=tf.contrib.layers.l2_regularizer(scale=1.0),
                            reuse=reuse, name=name, use_bias=not with_bn)
    return batch_normalization(dense, is_training, name + '_bn', reuse) if with_bn else dense
