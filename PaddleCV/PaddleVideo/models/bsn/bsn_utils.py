#  Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and

import numpy as np
from paddle.fluid.initializer import Uniform
from scipy.interpolate import interp1d
import pandas as pd
import multiprocessing as mp
import json
import pandas
import numpy
import random


def iou_with_anchors(anchors_min, anchors_max, box_min, box_max):
    """Compute jaccard score between a box and the anchors.
    """
    len_anchors = anchors_max - anchors_min
    int_xmin = np.maximum(anchors_min, box_min)
    int_xmax = np.minimum(anchors_max, box_max)
    inter_len = np.maximum(int_xmax - int_xmin, 0.)
    union_len = len_anchors - inter_len + box_max - box_min
    #print inter_len,union_len
    jaccard = np.divide(inter_len, union_len)
    return jaccard


def ioa_with_anchors(anchors_min, anchors_max, box_min, box_max):
    """Compute intersection between score a box and the anchors.
    """
    len_anchors = anchors_max - anchors_min
    int_xmin = np.maximum(anchors_min, box_min)
    int_xmax = np.minimum(anchors_max, box_max)
    inter_len = np.maximum(int_xmax - int_xmin, 0.)
    scores = np.divide(inter_len, len_anchors)
    return scores


def boundary_choose(score_list, peak_thres):
    max_score = max(score_list)
    mask_high = (score_list > max_score * peak_thres)
    score_list = list(score_list)
    score_middle = np.array([0.0] + score_list + [0.0])
    score_front = np.array([0.0, 0.0] + score_list)
    score_back = np.array(score_list + [0.0, 0.0])
    mask_peak = ((score_middle > score_front) & (score_middle > score_back))
    mask_peak = mask_peak[1:-1]
    mask = (mask_high | mask_peak).astype('float32')
    return mask


def soft_nms(df, alpha, t1, t2):
    df = df.sort_values(by="score", ascending=False)
    tstart = list(df.xmin.values[:])
    tend = list(df.xmax.values[:])
    tscore = list(df.score.values[:])

    rstart = []
    rend = []
    rscore = []

    while len(tscore) > 1 and len(rscore) < 101:
        max_index = tscore.index(max(tscore))
        tmp_iou_list = iou_with_anchors(
            np.array(tstart),
            np.array(tend), tstart[max_index], tend[max_index])
        for idx in range(0, len(tscore)):
            if idx != max_index:
                tmp_iou = tmp_iou_list[idx]
                tmp_width = tend[max_index] - tstart[max_index]
                if tmp_iou > t1 + (t2 - t1) * tmp_width:
                    tscore[idx] = tscore[idx] * np.exp(-np.square(tmp_iou) /
                                                       alpha)

        rstart.append(tstart[max_index])
        rend.append(tend[max_index])
        rscore.append(tscore[max_index])
        tstart.pop(max_index)
        tend.pop(max_index)
        tscore.pop(max_index)

    newDf = pd.DataFrame()
    newDf['score'] = rscore
    newDf['xmin'] = rstart
    newDf['xmax'] = rend
    return newDf


def video_process(video_list,
                  video_dict,
                  snms_alpha=0.75,
                  snms_t1=0.65,
                  snms_t2=0.9):

    for video_name in video_list:
        df = pd.read_csv("data/output/PEM_results/" + video_name + ".csv")
        df["score"] = df.xmin_score.values[:] * df.xmax_score.values[:] * df.iou_score.values[:]
        if len(df) > 1:
            df = soft_nms(df, snms_alpha, snms_t1, snms_t2)

        video_duration = video_dict[video_name]["duration_second"]
        proposal_list = []
        for idx in range(min(100, len(df))):
            tmp_prop={"score":df.score.values[idx],\
                      "segment":[max(0,df.xmin.values[idx])*video_duration,\
                                 min(1,df.xmax.values[idx])*video_duration]}
            proposal_list.append(tmp_prop)
        result_dict[video_name[2:]] = proposal_list


def bsn_post_processing(video_dict, subset):
    video_list = video_dict.keys()
    video_list = list(video_dict.keys())
    global result_dict
    result_dict = mp.Manager().dict()
    pp_num = 12

    num_videos = len(video_list)
    num_videos_per_thread = int(num_videos / pp_num)
    processes = []
    for tid in range(pp_num - 1):
        tmp_video_list = video_list[tid * num_videos_per_thread:(tid + 1) *
                                    num_videos_per_thread]
        p = mp.Process(
            target=video_process, args=(
                tmp_video_list,
                video_dict, ))
        p.start()
        processes.append(p)
    tmp_video_list = video_list[(pp_num - 1) * num_videos_per_thread:]
    p = mp.Process(
        target=video_process, args=(
            tmp_video_list,
            video_dict, ))
    p.start()
    processes.append(p)
    for p in processes:
        p.join()

    result_dict = dict(result_dict)
    output_dict = {
        "version": "VERSION 1.3",
        "results": result_dict,
        "external_data": {}
    }
    if subset == 'validation':
        outfile = open("data/evaluate_results/bsn_results_%s.json" % subset,
                       "w")
    elif subset == 'test':
        outfile = open("data/predict_results/bsn_results_%s.json" % subset, "w")
    json.dump(output_dict, outfile)
    outfile.close()


def generate_props(pgm_config, video_list, video_dict):
    tscale = pgm_config["tscale"]
    peak_thres = pgm_config["pgm_threshold"]
    anchor_xmin = [1.0 / tscale * i for i in range(tscale)]
    anchor_xmax = [1.0 / tscale * i for i in range(1, tscale + 1)]

    for video_name in video_list:
        video_info = video_dict[video_name]
        if video_info["subset"] == "training":
            top_K = pgm_config["pgm_top_K_train"]
        else:
            top_K = pgm_config["pgm_top_K"]

        tdf = pandas.read_csv("data/output/TEM_results/" + video_name + ".csv")
        start_scores = tdf.start.values[:]
        end_scores = tdf.end.values[:]

        start_mask = boundary_choose(start_scores, peak_thres)
        start_mask[0] = 1.
        end_mask = boundary_choose(end_scores, peak_thres)
        end_mask[-1] = 1.

        score_vector_list = []
        for idx in range(tscale):
            for jdx in range(tscale):
                start_index = jdx
                end_index = start_index + idx
                if end_index < tscale and start_mask[
                        start_index] == 1 and end_mask[end_index] == 1:
                    xmin = anchor_xmin[start_index]
                    xmax = anchor_xmax[end_index]
                    xmin_score = start_scores[start_index]
                    xmax_score = end_scores[end_index]
                    score_vector_list.append(
                        [xmin, xmax, xmin_score, xmax_score])
        num_data = len(score_vector_list)
        if num_data < top_K:
            for idx in range(top_K - num_data):
                start_index = random.randint(0, tscale - 1)
                end_index = random.randint(start_index, tscale - 1)
                xmin = anchor_xmin[start_index]
                xmax = anchor_xmax[end_index]
                xmin_score = start_scores[start_index]
                xmax_score = end_scores[end_index]
                score_vector_list.append([xmin, xmax, xmin_score, xmax_score])

        score_vector_list = np.stack(score_vector_list)
        col_name = ["xmin", "xmax", "xmin_score", "xmax_score"]
        new_df = pandas.DataFrame(score_vector_list, columns=col_name)
        new_df["score"] = new_df.xmin_score * new_df.xmax_score
        new_df = new_df.sort_values(by="score", ascending=False)
        new_df = new_df[:top_K]

        video_second = video_info['duration_second']

        try:
            gt_xmins = []
            gt_xmaxs = []
            for idx in range(len(video_info["annotations"])):
                gt_xmins.append(video_info["annotations"][idx]["segment"][0] /
                                video_second)
                gt_xmaxs.append(video_info["annotations"][idx]["segment"][1] /
                                video_second)

            new_iou_list = []
            for j in range(len(gt_xmins)):
                tmp_new_iou = iou_with_anchors(new_df.xmin.values[:],
                                               new_df.xmax.values[:],
                                               gt_xmins[j], gt_xmaxs[j])
                new_iou_list.append(tmp_new_iou)
            new_iou_list = numpy.stack(new_iou_list)
            new_iou_list = numpy.max(new_iou_list, axis=0)

            new_ioa_list = []
            for j in range(len(gt_xmins)):
                tmp_new_ioa = ioa_with_anchors(new_df.xmin.values[:],
                                               new_df.xmax.values[:],
                                               gt_xmins[j], gt_xmaxs[j])
                new_ioa_list.append(tmp_new_ioa)
            new_ioa_list = numpy.stack(new_ioa_list)
            new_ioa_list = numpy.max(new_ioa_list, axis=0)
            new_df["match_iou"] = new_iou_list
            new_df["match_ioa"] = new_ioa_list
        except:
            pass
        new_df.to_csv(
            "data/output/PGM_proposals/" + video_name + ".csv", index=False)


def generate_feats(pgm_config, video_list, video_dict):
    num_sample_start = pgm_config["num_sample_start"]
    num_sample_end = pgm_config["num_sample_end"]
    num_sample_action = pgm_config["num_sample_action"]
    num_sample_perbin = pgm_config["num_sample_perbin"]
    tscale = pgm_config["tscale"]
    seg_xmins = [1.0 / tscale * i for i in range(tscale)]
    seg_xmaxs = [1.0 / tscale * i for i in range(1, tscale + 1)]

    for video_name in video_list:
        adf = pandas.read_csv("data/output/TEM_results/" + video_name + ".csv")
        score_action = adf.action.values[:]
        video_scale = len(adf)
        video_gap = seg_xmaxs[0] - seg_xmins[0]
        video_extend = int(video_scale / 4 + 10)
        pdf = pandas.read_csv("data/output/PGM_proposals/" + video_name +
                              ".csv")
        tmp_zeros = numpy.zeros([video_extend])
        score_action = numpy.concatenate((tmp_zeros, score_action, tmp_zeros))
        tmp_cell = video_gap
        tmp_x = [-tmp_cell / 2 - (video_extend - 1 - ii) * tmp_cell for ii in range(video_extend)] + \
                [tmp_cell / 2 + ii * tmp_cell for ii in range(video_scale)] + \
                [tmp_cell / 2 + seg_xmaxs[-1] + ii * tmp_cell for ii in range(video_extend)]
        f_action = interp1d(tmp_x, score_action, axis=0)
        feature_bsp = []

        for idx in range(len(pdf)):
            xmin = pdf.xmin.values[idx]
            xmax = pdf.xmax.values[idx]
            xlen = xmax - xmin
            xmin_0 = xmin - xlen * pgm_config["bsp_boundary_ratio"]
            xmin_1 = xmin + xlen * pgm_config["bsp_boundary_ratio"]
            xmax_0 = xmax - xlen * pgm_config["bsp_boundary_ratio"]
            xmax_1 = xmax + xlen * pgm_config["bsp_boundary_ratio"]
            # start
            plen_start = (xmin_1 - xmin_0) / (num_sample_start - 1)
            plen_sample = plen_start / num_sample_perbin
            tmp_x_new = [
                xmin_0 - plen_start / 2 + plen_sample * ii
                for ii in range(num_sample_start * num_sample_perbin + 1)
            ]
            tmp_y_new_start_action = f_action(tmp_x_new)
            tmp_y_new_start = [
                numpy.mean(tmp_y_new_start_action[ii * num_sample_perbin:(ii + 1) * num_sample_perbin + 1]) \
                for ii in range(num_sample_start)]
            # end
            plen_end = (xmax_1 - xmax_0) / (num_sample_end - 1)
            plen_sample = plen_end / num_sample_perbin
            tmp_x_new = [
                xmax_0 - plen_end / 2 + plen_sample * ii
                for ii in range(num_sample_end * num_sample_perbin + 1)
            ]
            tmp_y_new_end_action = f_action(tmp_x_new)
            tmp_y_new_end = [
                numpy.mean(tmp_y_new_end_action[ii * num_sample_perbin:(ii + 1) * num_sample_perbin + 1]) \
                for ii in range(num_sample_end)]
            # action
            plen_action = (xmax - xmin) / (num_sample_action - 1)
            plen_sample = plen_action / num_sample_perbin
            tmp_x_new = [
                xmin - plen_action / 2 + plen_sample * ii
                for ii in range(num_sample_action * num_sample_perbin + 1)
            ]
            tmp_y_new_action = f_action(tmp_x_new)
            tmp_y_new_action = [
                numpy.mean(tmp_y_new_action[ii * num_sample_perbin:(ii + 1) * num_sample_perbin + 1]) \
                for ii in range(num_sample_action)]
            tmp_feature = numpy.concatenate(
                [tmp_y_new_action, tmp_y_new_start, tmp_y_new_end])
            feature_bsp.append(tmp_feature)
        feature_bsp = numpy.array(feature_bsp)
        numpy.save("data/output/PGM_feature/" + video_name, feature_bsp)


def pgm_gen_proposal(video_dict, pgm_config):
    video_list = list(video_dict.keys())
    video_list.sort()
    num_videos = len(video_list)
    num_videos_per_thread = int(num_videos / pgm_config["pgm_thread"])
    processes = []
    for tid in range(pgm_config["pgm_thread"] - 1):
        tmp_video_list = video_list[tid * num_videos_per_thread:(tid + 1) *
                                    num_videos_per_thread]
        p = mp.Process(
            target=generate_props,
            args=(
                pgm_config,
                tmp_video_list,
                video_dict, ))
        p.start()
        processes.append(p)
    tmp_video_list = video_list[(pgm_config["pgm_thread"] - 1) *
                                num_videos_per_thread:]
    p = mp.Process(
        target=generate_props, args=(
            pgm_config,
            tmp_video_list,
            video_dict, ))
    p.start()
    processes.append(p)
    for p in processes:
        p.join()


def pgm_gen_feature(video_dict, pgm_config):
    video_list = list(video_dict.keys())
    video_list.sort()
    num_videos = len(video_list)
    num_videos_per_thread = int(num_videos / pgm_config["pgm_thread"])
    processes = []
    for tid in range(pgm_config["pgm_thread"] - 1):
        tmp_video_list = video_list[tid * num_videos_per_thread:(tid + 1) *
                                    num_videos_per_thread]
        p = mp.Process(
            target=generate_feats,
            args=(
                pgm_config,
                tmp_video_list,
                video_dict, ))
        p.start()
        processes.append(p)

    tmp_video_list = video_list[(pgm_config["pgm_thread"] - 1) *
                                num_videos_per_thread:]
    p = mp.Process(
        target=generate_feats, args=(
            pgm_config,
            tmp_video_list,
            video_dict, ))
    p.start()
    processes.append(p)

    for p in processes:
        p.join()
