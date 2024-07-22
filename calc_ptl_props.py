import numexpr as ne
import numpy as np
from scipy.spatial import cKDTree
from colossus.cosmology import cosmology
import matplotlib as mpl
mpl.rcParams.update(mpl.rcParamsDefault)
import h5py
import pickle
import os
import multiprocessing as mp
from itertools import repeat
import sys
import re
import pandas as pd
import psutil

from utils.data_and_loading_functions import load_or_pickle_SPARTA_data, load_or_pickle_ptl_data, conv_halo_id_spid, get_comp_snap, create_directory, find_closest_z, timed, clean_dir
from utils.calculation_functions import *
from utils.visualization_functions import halo_plot_3d_vec, compare_density_prf
##################################################################################################################
# LOAD CONFIG PARAMETERS
import configparser
config = configparser.ConfigParser()
config.read(os.environ.get('PWD') + "/config.ini")
curr_sparta_file = config["MISC"]["curr_sparta_file"]
rand_seed = config.getint("MISC","random_seed")
path_to_MLOIS = config["PATHS"]["path_to_MLOIS"]
path_to_snaps = config["PATHS"]["path_to_snaps"]
path_to_SPARTA_data = config["PATHS"]["path_to_SPARTA_data"]
sim_pat = r"cbol_l(\d+)_n(\d+)"
match = re.search(sim_pat, curr_sparta_file)
if match:
    sparta_name = match.group(0)
snap_loc = path_to_snaps + sparta_name + "/"
path_to_hdf5_file = path_to_SPARTA_data + sparta_name + "/" + curr_sparta_file + ".hdf5"
path_to_pickle = config["PATHS"]["path_to_pickle"]
path_to_calc_info = config["PATHS"]["path_to_calc_info"]
path_to_pygadgetreader = config["PATHS"]["path_to_pygadgetreader"]
path_to_sparta = config["PATHS"]["path_to_sparta"]
create_directory(path_to_pickle)
create_directory(path_to_calc_info)
snap_format = config["MISC"]["snap_format"]
reset_lvl = config.getint("SEARCH","reset")
global prim_only
prim_only = config.getboolean("SEARCH","prim_only")
t_dyn_step = config.getfloat("SEARCH","t_dyn_step")
global p_red_shift
p_red_shift = config.getfloat("SEARCH","p_red_shift")
global search_rad
search_rad = config.getfloat("SEARCH","search_rad")
total_num_snaps = config.getint("SEARCH","total_num_snaps")
per_n_halo_per_split = config.getfloat("SEARCH","per_n_halo_per_split")
# num_processes = int(os.environ['SLURM_CPUS_PER_TASK'])
num_processes = mp.cpu_count()
curr_chunk_size = config.getint("SEARCH","chunk_size")
test_halos_ratio = config.getfloat("XGBOOST","test_halos_ratio")
mem_size = config.getfloat("SEARCH","hdf5_mem_size")
##################################################################################################################
sys.path.insert(1, path_to_pygadgetreader)
sys.path.insert(1, path_to_sparta)
from pygadgetreader import readsnap, readheader # type: ignore
from sparta_tools import sparta # type: ignore
##################################################################################################################
global count
count = 0

def memory_usage():
    process = psutil.Process(os.getpid())
    return process.memory_info().rss

def find_start_pnt(directory):
    max_number = 0
    number_pattern = re.compile(r'(\d+)')
    
    for filename in os.listdir(directory):
        match = number_pattern.search(filename)
        if match:
            number = int(match.group(1)) + 1
            if number > max_number:
                max_number = number
                
    return max_number

def calc_halo_mem(n_ptl):
    # rad, rad_vel, tang_vel each 4bytes and two snaps
    # HIPIDS is 8 bytes 
    # orbit/infall is one byte
    n_bytes = (6 * 4 + 1 + 8) * n_ptl
    
    return n_bytes

def det_halo_splits(mem_arr, mem_lim):
    split_idxs = []
    split_idxs.append(0)
    curr_size = 0 
    
    for i,mem in enumerate(mem_arr):
        curr_size += mem
        # Take into 128bytes for index in pandas dataframe
        if (curr_size + 128) > mem_lim:
            split_idxs.append(i)
            curr_size = 0
    return split_idxs
            
def initial_search(halo_positions, halo_r200m, comp_snap, find_mass = False, find_ptl_indices = False):
    if comp_snap:
        tree = c_ptl_tree
    else:
        tree = p_ptl_tree

    if halo_r200m > 0:
        #find how many particles we are finding
        indices = tree.query_ball_point(halo_positions, r = search_rad * halo_r200m)
        indices = np.array(indices)
        # how many new particles being added and correspondingly how massive the halo is
        num_new_particles = indices.shape[0]

        if find_mass:
            halo_mass = num_new_particles * mass
        if find_ptl_indices:
            all_ptl_indices = indices
    else:
        num_new_particles = 0
        all_ptl_indices = np.empty(1)
    
    if find_mass == False and find_ptl_indices == False:
        return num_new_particles
    elif find_mass == True and find_ptl_indices == False:
        return num_new_particles, halo_mass
    elif find_mass == False and find_ptl_indices == True:
        return num_new_particles, all_ptl_indices
    else:
        return num_new_particles, halo_mass, all_ptl_indices
    
def search_halos(comp_snap, snap_dict, curr_halo_idx, curr_ptl_pids, curr_ptl_pos, curr_ptl_vel, 
                 halo_pos, halo_vel, halo_r200m, sparta_last_pericenter_snap=None, sparta_n_pericenter=None, sparta_tracer_ids=None,
                 sparta_n_is_lower_limit=None, dens_prf_all=None, dens_prf_1halo=None, bins=None, create_dens_prf=False):
    # Doing this this way as otherwise will have to generate super large arrays for input from multiprocessing
    snap = snap_dict["snap"]
    red_shift = snap_dict["red_shift"]
    scale_factor = snap_dict["scale_factor"]
    hubble_const = snap_dict["hubble_const"]
    box_size = snap_dict["box_size"] 
    little_h = snap_dict["h"]   
    
    halo_pos = halo_pos * 10**3 * scale_factor
    
    num_new_ptls = curr_ptl_pids.shape[0]

    curr_ptl_pids = curr_ptl_pids.astype(np.int64) # otherwise ne.evaluate doesn't work
    fnd_HIPIDs = ne.evaluate("0.5 * (curr_ptl_pids + curr_halo_idx) * (curr_ptl_pids + curr_halo_idx + 1) + curr_halo_idx")
    
    #calculate the radii of each particle based on the distance formula
    ptl_rad, coord_dist = calculate_distance(halo_pos[0], halo_pos[1], halo_pos[2], curr_ptl_pos[:,0], curr_ptl_pos[:,1], curr_ptl_pos[:,2], num_new_ptls, box_size)         
    
    if comp_snap == False:         
        compare_sparta_assn = np.zeros((sparta_tracer_ids.shape[0]))
        curr_orb_assn = np.zeros((num_new_ptls))
         # Anywhere sparta_last_pericenter is greater than the current snap then that is in the future so set to 0
        future_peri = np.where(sparta_last_pericenter_snap > snap)[0]
        adj_sparta_n_pericenter = sparta_n_pericenter
        adj_sparta_n_pericenter[future_peri] = 0
        adj_sparta_n_is_lower_limit = sparta_n_is_lower_limit
        adj_sparta_n_is_lower_limit[future_peri] = 0
        # If a particle has a pericenter or if the lower limit is 1 then it is orbiting

        compare_sparta_assn[np.where((adj_sparta_n_pericenter >= 1) | (adj_sparta_n_is_lower_limit == 1))[0]] = 1
        # compare_sparta_assn[np.where(adj_sparta_n_pericenter >= 1)] = 1
        
        # Compare the ids between SPARTA and the found prtl ids and match the SPARTA results
        matched_ids = np.intersect1d(curr_ptl_pids, sparta_tracer_ids, return_indices = True)
        curr_orb_assn[matched_ids[1]] = compare_sparta_assn[matched_ids[2]]

    # calculate peculiar, radial, and tangential velocity
    pec_vel = calc_pec_vel(curr_ptl_vel, halo_vel)
    fnd_rad_vel, curr_v200m, phys_vel, phys_vel_comp, rhat = calc_rad_vel(pec_vel, ptl_rad, coord_dist, halo_r200m, red_shift, hubble_const, little_h)
    fnd_tang_vel = calc_tang_vel(fnd_rad_vel, phys_vel_comp, rhat)
    
    scaled_rad_vel = fnd_rad_vel / curr_v200m
    scaled_tang_vel = fnd_tang_vel / curr_v200m
    scaled_radii = ptl_rad / halo_r200m
    scaled_phys_vel = phys_vel / curr_v200m
    
    # vel_scal = 1
    # per_of_halo = 0.05
    # if comp_snap == False:
    #     # particles with very high radial velocities at small radii should be considered infalling
    #     # incorrectly classified
    #     curr_orb_assn[np.where((scaled_radii < 1.1) & (phys_vel>(vel_scal*curr_v200m)))] = 0
    #     if np.where((scaled_radii < 1.1) & (phys_vel>(vel_scal*curr_v200m)))[0].shape[0] > int(np.floor(per_of_halo * ptl_rad.shape[0])):
    #         constraint = np.where((scaled_radii < 1.1) & (phys_vel>(vel_scal*curr_v200m)))[0]
    #         halo_plot_3d_vec(curr_ptl_pos, curr_ptl_vel, halo_pos, halo_vel, halo_r200m, curr_orb_assn, constraint, curr_halo_idx,vel_scal)
        
    scaled_radii_inds = scaled_radii.argsort()
    scaled_radii = scaled_radii[scaled_radii_inds]
    fnd_HIPIDs = fnd_HIPIDs[scaled_radii_inds]
    scaled_rad_vel = scaled_rad_vel[scaled_radii_inds]
    scaled_tang_vel = scaled_tang_vel[scaled_radii_inds]
    scaled_phys_vel = scaled_phys_vel[scaled_radii_inds]

    if comp_snap == False:
        curr_orb_assn = curr_orb_assn[scaled_radii_inds]

    global count
    if create_dens_prf:
        count += 1
        with open(path_to_pickle + "190_cbol_l0063_n0256_10r200m/ptl_mass.pickle", "rb") as pickle_file:
            ptl_mass = pickle.load(pickle_file)

        bins = np.insert(bins, 0, 0)
        dens_prf_all = np.reshape(dens_prf_all,(1,80))
        dens_prf_1halo = np.reshape(dens_prf_1halo,(1,80))

        compare_density_prf(np.array([0]),scaled_radii,np.array([0]),np.array([num_new_ptls]),dens_prf_all,dens_prf_1halo,np.array([ptl_mass]),curr_orb_assn,bins,str(curr_halo_idx),"/home/zvladimi/MLOIS/Random_figs/",save_graph=True)

    if comp_snap == False:
        return fnd_HIPIDs, curr_orb_assn, scaled_rad_vel, scaled_tang_vel, scaled_radii, scaled_phys_vel
    else:
        return fnd_HIPIDs, scaled_rad_vel, scaled_tang_vel, scaled_radii

def halo_loop(halo_idx,ptl_idx,curr_iter,num_iter,rst_pnt, indices, halo_splits, dst_name, tot_num_ptls, p_halo_ids, p_dict, p_ptls_pid, p_ptls_pos, p_ptls_vel, c_dict, c_ptls_pid, c_ptls_pos, c_ptls_vel):
        print(f"Initial memory usage: {memory_usage() / 1024**3:.2f} GB")
        with timed("Split "+str(curr_iter+1)+"/"+str(num_iter)):
            # Get the indices corresponding to where we are in the number of iterations (0:num_halo_persplit) -> (num_halo_persplit:2*num_halo_persplit) etc
            if curr_iter < (num_iter - 1):
                use_indices = indices[halo_splits[curr_iter]:halo_splits[curr_iter+1]]
            else:
                use_indices = indices[halo_splits[curr_iter]:]
            
            curr_num_halos = use_indices.shape[0]
            use_halo_ids = p_halo_ids[use_indices]
            # print(use_halo_ids[10])
            # Load the halo information for the ids within this range
            sparta_output = sparta.load(filename = path_to_hdf5_file, halo_ids=use_halo_ids, log_level=0)

            global c_sparta_snap
            c_sparta_snap = np.abs(dic_sim["snap_z"][:] - c_red_shift).argmin()
            
            new_idxs = conv_halo_id_spid(use_halo_ids, sparta_output, p_sparta_snap) # If the order changed by sparta re-sort the indices
            use_halo_idxs = use_indices[new_idxs]

            # Search around these halos and get the number of particles and the corresponding ptl indices for them
            p_use_halos_pos = sparta_output['halos']['position'][:,p_sparta_snap] * 10**3 * p_scale_factor 
            p_use_halos_r200m = sparta_output['halos']['R200m'][:,p_sparta_snap]

            
            with mp.Pool(processes=num_processes) as p:
                # halo position, halo r200m, if comparison snap, want mass?, want indices?
                p_use_num_ptls, p_curr_ptl_indices = zip(*p.starmap(initial_search, zip(p_use_halos_pos, p_use_halos_r200m, repeat(False), repeat(False), repeat(True)), chunksize=curr_chunk_size))
            p.close()
            p.join() 
            
            # Remove halos with 0 ptls around them
            p_use_num_ptls = np.array(p_use_num_ptls)
            p_curr_ptl_indices = np.array(p_curr_ptl_indices, dtype=object)
            has_ptls = np.where(p_use_num_ptls > 0)
            p_use_num_ptls = p_use_num_ptls[has_ptls]
            p_curr_ptl_indices = p_curr_ptl_indices[has_ptls]
            p_use_halo_idxs = use_halo_idxs[has_ptls]
            p_curr_num_halos = p_curr_ptl_indices.shape[0]

            # We need to correct for having previous halos being searched so the final halo_first quantity is for all halos
            # First obtain the halo_first values for this batch and then adjust to where the hdf5 file currently is
            p_start_num_ptls = np.cumsum(p_use_num_ptls)
            p_start_num_ptls = np.insert(p_start_num_ptls, 0, 0)
            p_start_num_ptls = np.delete(p_start_num_ptls, -1)
            p_start_num_ptls += ptl_idx # scale to where we are in the hdf5 file
            
            p_tot_num_use_ptls = int(np.sum(p_use_num_ptls))

            halo_first = sparta_output['halos']['ptl_oct_first'][:]
            halo_n = sparta_output['halos']['ptl_oct_n'][:]
            
            # Use multiprocessing to search multiple halos at the same time and add information to shared arrays
            with mp.Pool(processes=num_processes) as p:
                p_all_HIPIDs, p_all_orb_assn, p_all_rad_vel, p_all_tang_vel, p_all_scal_rad, p_phys_vel = zip(*p.starmap(search_halos, 
                                            zip(repeat(False), repeat(p_dict), p_use_halo_idxs,
                                            (p_ptls_pid[p_curr_ptl_indices[i]] for i in range(p_curr_num_halos)), 
                                            (p_ptls_pos[p_curr_ptl_indices[j]] for j in range(p_curr_num_halos)),
                                            (p_ptls_vel[p_curr_ptl_indices[k]] for k in range(p_curr_num_halos)),
                                            (sparta_output['halos']['position'][l,p_sparta_snap,:] for l in range(p_curr_num_halos)),
                                            (sparta_output['halos']['velocity'][l,p_sparta_snap,:] for l in range(p_curr_num_halos)),
                                            (sparta_output['halos']['R200m'][l,p_sparta_snap] for l in range(p_curr_num_halos)),
                                            (sparta_output['tcr_ptl']['res_oct']['last_pericenter_snap'][halo_first[m]:halo_first[m]+halo_n[m]] for m in range(p_curr_num_halos)),
                                            (sparta_output['tcr_ptl']['res_oct']['n_pericenter'][halo_first[m]:halo_first[m]+halo_n[m]] for m in range(p_curr_num_halos)),
                                            (sparta_output['tcr_ptl']['res_oct']['tracer_id'][halo_first[m]:halo_first[m]+halo_n[m]] for m in range(p_curr_num_halos)),
                                            (sparta_output['tcr_ptl']['res_oct']['n_is_lower_limit'][halo_first[m]:halo_first[m]+halo_n[m]] for m in range(p_curr_num_halos)),
                                            (sparta_output['anl_prf']['M_all'][l,p_sparta_snap,:] for l in range(p_curr_num_halos)),
                                            (sparta_output['anl_prf']['M_1halo'][l,p_sparta_snap,:] for l in range(p_curr_num_halos)),
                                            # Uncomment below to create dens profiles
                                            # repeat(sparta_output["config"]['anl_prf']["r_bins_lin"]),repeat(True) 
                                            ),chunksize=curr_chunk_size))
            p.close()
            p.join()
            
            p_all_HIPIDs = np.concatenate(p_all_HIPIDs, axis = 0)
            p_all_orb_assn = np.concatenate(p_all_orb_assn, axis = 0)
            p_all_rad_vel = np.concatenate(p_all_rad_vel, axis = 0)
            p_all_tang_vel = np.concatenate(p_all_tang_vel, axis = 0)
            p_all_scal_rad = np.concatenate(p_all_scal_rad, axis = 0)
            p_phys_vel = np.concatenate(p_phys_vel, axis=0)
            
            p_all_HIPIDs = p_all_HIPIDs.astype(np.float64)
            p_all_orb_assn = p_all_orb_assn.astype(np.int8)
            p_all_rad_vel = p_all_rad_vel.astype(np.float32)
            p_all_tang_vel = p_all_tang_vel.astype(np.float32)
            p_all_scal_rad = p_all_scal_rad.astype(np.float32)
            p_phys_vel = p_phys_vel.astype(np.float32)
            
            num_bins = 30
        
            # If multiple snaps also search the comparison snaps in the same manner as with the primary snap
            if prim_only == False:
                c_use_halos_pos = sparta_output['halos']['position'][:,c_sparta_snap] * 10**3 * c_scale_factor
                c_use_halos_r200m = sparta_output['halos']['R200m'][:,c_sparta_snap] 

                with mp.Pool(processes=num_processes) as p:
                    # halo position, halo r200m, if comparison snap, if train dataset, want mass?, want indices?
                    c_use_num_ptls, c_curr_ptl_indices = zip(*p.starmap(initial_search, zip(c_use_halos_pos, c_use_halos_r200m, repeat(True), repeat(False), repeat(True)), chunksize=curr_chunk_size))
                p.close()
                p.join() 

                c_use_num_ptls = np.array(c_use_num_ptls)
                c_curr_ptl_indices = np.array(c_curr_ptl_indices, dtype=object)
                has_ptls = np.where(c_use_num_ptls > 0)[0]
                c_use_num_ptls = c_use_num_ptls[has_ptls]
                c_curr_ptl_indices = c_curr_ptl_indices[has_ptls]
                c_use_halo_idxs = use_halo_idxs[has_ptls]
                c_tot_num_use_ptls = int(np.sum(c_use_num_ptls))
                c_curr_num_halos = c_curr_ptl_indices.shape[0]
                
                
                with mp.Pool(processes=num_processes) as p:
                    c_all_HIPIDs, c_all_rad_vel, c_all_tang_vel, c_all_scal_rad = zip(*p.starmap(search_halos, 
                                                zip(repeat(True), repeat(c_dict),c_use_halo_idxs,
                                                    (c_ptls_pid[c_curr_ptl_indices[i]] for i in range(c_curr_num_halos)), 
                                                    (c_ptls_pos[c_curr_ptl_indices[j]] for j in range(c_curr_num_halos)),
                                                    (c_ptls_vel[c_curr_ptl_indices[k]] for k in range(c_curr_num_halos)),
                                                    (sparta_output['halos']['position'][l,c_sparta_snap,:] for l in range(c_curr_num_halos)),
                                                    (sparta_output['halos']['velocity'][l,c_sparta_snap,:] for l in range(c_curr_num_halos)),
                                                    (sparta_output['halos']['R200m'][l,c_sparta_snap] for l in range(c_curr_num_halos)),
                                                    ),chunksize=curr_chunk_size))
                p.close()
                p.join()
                
                c_all_HIPIDs = np.concatenate(c_all_HIPIDs, axis = 0)
                c_all_rad_vel = np.concatenate(c_all_rad_vel, axis = 0)
                c_all_tang_vel = np.concatenate(c_all_tang_vel, axis = 0)
                c_all_scal_rad = np.concatenate(c_all_scal_rad, axis = 0)
                
                
                c_all_HIPIDs = c_all_HIPIDs.astype(np.float64)
                c_all_rad_vel = c_all_rad_vel.astype(np.float32)
                c_all_tang_vel = c_all_tang_vel.astype(np.float32)
                c_all_scal_rad = c_all_scal_rad.astype(np.float32)
                
                use_max_shape = (tot_num_ptls,2)                  
                save_scale_radii = np.zeros((p_tot_num_use_ptls,2), dtype = np.float32)
                save_rad_vel = np.zeros((p_tot_num_use_ptls,2), dtype = np.float32)
                save_tang_vel = np.zeros((p_tot_num_use_ptls,2), dtype = np.float32)

                # Match the PIDs from primary snap to the secondary snap
                # If they don't have a match set those as np.NaN for xgboost 
                match_hipid_idx = np.intersect1d(p_all_HIPIDs, c_all_HIPIDs, return_indices=True)
                save_scale_radii[:,0] = p_all_scal_rad
                save_scale_radii[match_hipid_idx[1],1] = c_all_scal_rad[match_hipid_idx[2]]
                save_rad_vel[:,0] = p_all_rad_vel
                save_rad_vel[match_hipid_idx[1],1] = c_all_rad_vel[match_hipid_idx[2]]
                save_tang_vel[:,0] = p_all_tang_vel
                save_tang_vel[match_hipid_idx[1],1] = c_all_tang_vel[match_hipid_idx[2]]
                
                save_scale_radii[save_scale_radii[:,1] == 0, 1] = np.NaN
                save_rad_vel[save_rad_vel[:,1] == 0, 1] = np.NaN
                save_tang_vel[save_tang_vel[:,1] == 0, 1] = np.NaN
            
            else:
                use_max_shape = (tot_num_ptls)  
                save_scale_radii = np.zeros(p_tot_num_use_ptls)
                save_rad_vel = np.zeros(p_tot_num_use_ptls)
                save_tang_vel = np.zeros(p_tot_num_use_ptls)
                
                save_scale_radii = p_all_scal_rad
                save_rad_vel = p_all_rad_vel
                save_tang_vel = p_all_tang_vel
            
            halo_df = pd.DataFrame({
                "Halo_first":p_start_num_ptls,
                "Halo_n":p_use_num_ptls,
                "Halo_indices":use_indices,
            })
            
            ptl_df = pd.DataFrame({
                "HIPIDS":p_all_HIPIDs,
                "Orbit_infall":p_all_orb_assn,
                "p_Scaled_radii":save_scale_radii[:,0],
                "p_Radial_vel":save_rad_vel[:,0],
                "p_Tangential_vel":save_tang_vel[:,0],
                "c_Scaled_radii":save_scale_radii[:,1],
                "c_Radial_vel":save_rad_vel[:,1],
                "c_Tangential_vel":save_tang_vel[:,1],
                "p_phys_vel":p_phys_vel
                })
            
            print(np.where(p_all_orb_assn == 0)[0].shape[0] / np.where(p_all_orb_assn == 1)[0].shape[0])
            sim_splits = [0]
            # compare_density_prf(sim_splits,radii=ptl_df ["p_Scaled_radii"].values, halo_first=halo_df["Halo_first"], halo_n=halo_df["Halo_n"], act_mass_prf_all=dens_prf_all, act_mass_prf_orb=dens_prf_1halo, mass=all_masses, orbit_assn=preds, prf_bins=bins, title="", save_location=plot_save_loc, use_mp=True, save_graph=True)
            
            halo_df.to_hdf(save_location + dst_name + "/halo_info/halo_" + str(curr_iter+rst_pnt) + ".h5", key='data', mode='w',format='table')  
            ptl_df.to_hdf(save_location +  dst_name + "/ptl_info/ptl_" + str(curr_iter+rst_pnt) + ".h5", key='data', mode='w',format='table')  

            ptl_idx += p_tot_num_use_ptls
            halo_idx += p_start_num_ptls.shape[0]
            del sparta_output   
        print(f"Final memory usage: {memory_usage() / 1024**3:.2f} GB")
        return halo_idx, ptl_idx
                
with timed("Startup"):
    cosmol = cosmology.setCosmology("bolshoi") 

    with timed("p_snap information load"):
        p_snap, p_red_shift = find_closest_z(p_red_shift,snap_loc,snap_format)
        print("Snapshot number found:", p_snap, "Closest redshift found:", p_red_shift)
        with h5py.File(path_to_hdf5_file,"r") as f:
            dic_sim = {}
            grp_sim = f['simulation']
            for f in grp_sim.attrs:
                dic_sim[f] = grp_sim.attrs[f]
            
        all_red_shifts = dic_sim['snap_z']
        p_sparta_snap = np.abs(all_red_shifts - p_red_shift).argmin()
        print("corresponding SPARTA snap num:", p_sparta_snap)
        print("check sparta redshift:",all_red_shifts[p_sparta_snap])   

        # Set constants
        p_snapshot_path = snap_loc + "snapdir_" + snap_format.format(p_snap) + "/snapshot_" + snap_format.format(p_snap)

        p_scale_factor = 1/(1+p_red_shift)
        p_rho_m = cosmol.rho_m(p_red_shift)
        p_hubble_constant = cosmol.Hz(p_red_shift) * 0.001 # convert to units km/s/kpc
        sim_box_size = dic_sim["box_size"] #units Mpc/h comoving
        p_box_size = sim_box_size * 10**3 * p_scale_factor #convert to Kpc/h physical
        little_h = dic_sim["h"]

        p_snap_dict = {
            "snap":p_snap,
            "red_shift":p_red_shift,
            "scale_factor": p_scale_factor,
            "hubble_const": p_hubble_constant,
            "box_size": p_box_size,
            "h":little_h
        }
    print(little_h)
    if reset_lvl == 3:
        clean_dir(path_to_pickle + str(p_snap) + "_" + str(sparta_name) + "_" + str(int(search_rad)) + "r200m/")

    # load all information needed for the primary snap
    with timed("p_snap ptl load"):
        p_ptls_pid, p_ptls_vel, p_ptls_pos = load_or_pickle_ptl_data(curr_sparta_file, str(p_snap), p_snapshot_path, p_scale_factor)

    with timed("p_snap SPARTA load"):
        global mass
        p_halos_pos, p_halos_r200m, p_halos_id, p_halos_status, p_halos_last_snap, p_parent_id, mass = load_or_pickle_SPARTA_data(curr_sparta_file, p_scale_factor, p_snap, p_sparta_snap)

    with timed("c_snap load"):
        t_dyn = calc_t_dyn(p_halos_r200m[np.where(p_halos_r200m > 0)[0][0]], p_red_shift)
        c_snap, c_sparta_snap, c_rho_m, c_red_shift, c_scale_factor, c_hubble_constant, c_ptls_pid, c_ptls_vel, c_ptls_pos, c_halos_pos, c_halos_r200m, c_halos_id, c_halos_status, c_halos_last_snap = get_comp_snap(t_dyn=t_dyn, t_dyn_step=t_dyn_step, snapshot_list=[p_snap], cosmol = cosmol, p_red_shift=p_red_shift, all_red_shifts=all_red_shifts, snap_format=snap_format,snap_loc=snap_loc)
        c_box_size = sim_box_size * 10**3 * c_scale_factor #convert to Kpc/h physical

    c_snap_dict = {
        "snap":c_snap,
        "red_shift":c_red_shift,
        "scale_factor": c_scale_factor,
        "hubble_const": c_hubble_constant,
        "box_size": c_box_size,
        "h":little_h
    }

    snapshot_list = [p_snap, c_snap]

    if prim_only:
        save_location =  path_to_calc_info + curr_sparta_file + "_" + str(snapshot_list[0]) + "/"
    else:
        save_location =  path_to_calc_info + curr_sparta_file + "_" + str(snapshot_list[0]) + "to" + str(snapshot_list[1]) + "/"

    if os.path.exists(save_location) != True:
        os.makedirs(save_location)

    if os.path.isfile(save_location + "p_ptl_tree.pickle") and reset_lvl < 2:
            with open(save_location + "p_ptl_tree.pickle", "rb") as pickle_file:
                p_ptl_tree = pickle.load(pickle_file)
    else:
        p_ptl_tree = cKDTree(data = p_ptls_pos, leafsize = 3, balanced_tree = False, boxsize = p_box_size) # construct search trees for primary snap
        with open(save_location + "p_ptl_tree.pickle", "wb") as pickle_file:
            pickle.dump(p_ptl_tree, pickle_file)
            
    if os.path.isfile(save_location + "c_ptl_tree.pickle") and reset_lvl < 2:
            with open(save_location + "c_ptl_tree.pickle", "rb") as pickle_file:
                c_ptl_tree = pickle.load(pickle_file)
    else:
        c_ptl_tree = cKDTree(data = c_ptls_pos, leafsize = 3, balanced_tree = False, boxsize = c_box_size)
        with open(save_location + "c_ptl_tree.pickle", "wb") as pickle_file:
            pickle.dump(c_ptl_tree, pickle_file)


    if os.path.isfile(save_location + "num_ptls.pickle") and os.path.isfile(save_location + "match_halo_idxs.pickle") and reset_lvl < 2:
        with open(save_location + "num_ptls.pickle", "rb") as pickle_file:
            num_ptls = pickle.load(pickle_file)
        with open(save_location + "match_halo_idxs.pickle", "rb") as pickle_file:
            match_halo_idxs = pickle.load(pickle_file)
    else:
        # only take halos that are hosts in primary snap and exist past the p_snap and exist in some form at the comparison snap
        if prim_only:
            match_halo_idxs = np.where((p_halos_status == 10) & (p_halos_last_snap >= p_sparta_snap))[0]
        else:
            match_halo_idxs = np.where((p_halos_status == 10) & (p_halos_last_snap >= p_sparta_snap) & (c_halos_status > 0) & (c_halos_last_snap >= c_sparta_snap))[0]
        with mp.Pool(processes=num_processes) as p:           
            # halo position, halo r200m, if comparison snap, want mass?, want indices?
            num_ptls = p.starmap(initial_search, zip(p_halos_pos[match_halo_idxs], p_halos_r200m[match_halo_idxs], repeat(False), repeat(False), repeat(False)), chunksize=curr_chunk_size)
            
            # We want to remove any halos that have less than 200 particles as they are too noisy
            num_ptls = np.array(num_ptls)
            res_mask = np.where(num_ptls >= 200)[0]

            if res_mask.size > 0:
                num_ptls = num_ptls[res_mask]
                match_halo_idxs = match_halo_idxs[res_mask]
            
            with open(save_location + "num_ptls.pickle", "wb") as pickle_file:
                pickle.dump(num_ptls, pickle_file)
            with open(save_location + "match_halo_idxs.pickle", "wb") as pickle_file:
                pickle.dump(match_halo_idxs, pickle_file)
        p.close()
        p.join() 
    tot_num_ptls = np.sum(num_ptls)
    
    total_num_halos = match_halo_idxs.shape[0]
    
    # rng = np.random.default_rng(rand_seed)    
    # total_num_halos = match_halo_idxs.shape[0]
    # rng.shuffle(match_halo_idxs)
    # split all indices into train and test groups
    split_pnt = int((1-test_halos_ratio) * total_num_halos)
    train_idxs = match_halo_idxs[:split_pnt]
    test_idxs = match_halo_idxs[split_pnt:]
    train_num_ptls = num_ptls[:split_pnt]
    test_num_ptls = num_ptls[split_pnt:]

    # need to sort indices otherwise sparta.load breaks...
    train_idxs_inds = train_idxs.argsort()
    train_idxs = train_idxs[train_idxs_inds]
    train_num_ptls = train_num_ptls[train_idxs_inds]
    
    test_idxs_inds = test_idxs.argsort()
    test_idxs = test_idxs[test_idxs_inds]
    test_num_ptls = test_num_ptls[test_idxs_inds]
    
    train_halo_mem = calc_halo_mem(train_num_ptls)
    test_halo_mem = calc_halo_mem(test_num_ptls)

    train_halo_splits = det_halo_splits(train_halo_mem, mem_size)
    test_halo_splits = det_halo_splits(test_halo_mem, mem_size)
        
    print(f"Total num halos: {total_num_halos:.3e}")
    print(f"Total num ptls: {tot_num_ptls:.3e}")

    #TODO add functionality to check if all the params are the same and then restart (and throw warning) or maybe just reload them if restart=0
    config_params = {
        "sparta_file": curr_sparta_file,
        "snap_format": snap_format,
        "prim_only": prim_only,
        "t_dyn_step": t_dyn_step,
        "p_red_shift":p_red_shift,
        "search_rad": search_rad,
        "total_num_snaps": total_num_snaps,
        "per_n_halo_per_split": per_n_halo_per_split,
        "chunk_size": curr_chunk_size,
        "HDF5 Mem Size": mem_size,
        "p_snap_info": p_snap_dict,
        "c_snap_info": c_snap_dict,
    }

    with open((save_location + "config.pickle"), 'wb') as f:
        pickle.dump(config_params,f)
        
    create_directory(save_location + "Train/halo_info/")
    create_directory(save_location + "Train/ptl_info/")
    create_directory(save_location + "Test/halo_info/")
    create_directory(save_location + "Test/ptl_info/")
    if reset_lvl > 0: # At any level of reset we delete the calculated info for particles
        clean_dir(save_location + "Train/halo_info/")
        clean_dir(save_location + "Train/ptl_info/")
        clean_dir(save_location + "Test/halo_info/")
        clean_dir(save_location + "Test/ptl_info/")
        train_start_pnt=0
        test_start_pnt=0

    else: #Otherwise check to see where we were and then continue the calculations from there.
        train_start_pnt = find_start_pnt(save_location + "Train/ptl_info/")
        test_start_pnt = find_start_pnt(save_location + "Test/ptl_info/")
        train_halo_splits = train_halo_splits[train_start_pnt:]
        test_halo_splits = test_halo_splits[test_start_pnt:]
        
    prnt_halo_splits = train_halo_splits.copy()
    prnt_halo_splits.append(len(train_idxs))        
    
    for i in range(len(prnt_halo_splits)):
        if i < len(prnt_halo_splits) - 1:
            print(((np.sum(train_halo_mem[prnt_halo_splits[i]:prnt_halo_splits[i+1]])) + 128 )*1e-9,"GB")
        
with timed("Finished Calc"):   
    train_num_iter = len(train_halo_splits)
    train_prnt_halo_splits = train_halo_splits.copy()
    train_prnt_halo_splits.append(train_idxs.size)
    print("Train Splits")
    print("Num halos in each split:", ", ".join(map(str, np.diff(prnt_halo_splits))) + ".", train_num_iter, "splits")
    ptl_idx = 0
    halo_idx = 0
    
    for i in range(train_num_iter):
        halo_idx, ptl_idx = halo_loop(halo_idx=halo_idx,ptl_idx=ptl_idx,curr_iter=i,num_iter=train_num_iter,rst_pnt=train_start_pnt,indices=train_idxs, halo_splits=train_halo_splits, dst_name="Train", tot_num_ptls=tot_num_ptls, p_halo_ids=p_halos_id, p_dict=p_snap_dict, p_ptls_pid=p_ptls_pid, p_ptls_pos=p_ptls_pos, p_ptls_vel=p_ptls_vel, c_dict=c_snap_dict, c_ptls_pid=c_ptls_pid, c_ptls_pos=c_ptls_pos, c_ptls_vel=c_ptls_vel)
    
    test_num_iter = len(test_halo_splits)
    test_prnt_halo_splits = test_halo_splits.copy()
    test_prnt_halo_splits.append(test_idxs.size)
    print("Test Splits")
    print("Num halos in each split:", ", ".join(map(str, np.diff(prnt_halo_splits))) + ".", test_num_iter, "splits")
    ptl_idx = 0
    halo_idx = 0
    for i in range(test_num_iter):
        halo_idx, ptl_idx = halo_loop(halo_idx=halo_idx,ptl_idx=ptl_idx,curr_iter=i,num_iter=test_num_iter,rst_pnt=test_start_pnt,indices=test_idxs, halo_splits=test_halo_splits, dst_name="Test", tot_num_ptls=tot_num_ptls, p_halo_ids=p_halos_id, p_dict=p_snap_dict, p_ptls_pid=p_ptls_pid, p_ptls_pos=p_ptls_pos, p_ptls_vel=p_ptls_vel, c_dict=c_snap_dict, c_ptls_pid=c_ptls_pid, c_ptls_pos=c_ptls_pos, c_ptls_vel=c_ptls_vel)
