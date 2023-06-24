"""
MedTextCleaner - An Orthanc plugin for text removal in medical images

This program is free software: you can redistribute it and/or
modify it under the terms of the GNU General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np
import itertools
from math import sqrt

import torch
import torch.nn.functional as F
from torchvision.ops.boxes import box_iou, box_convert


class Encoder(object):
    """
        Inspired by https://github.com/kuangliu/pytorch-src
        Transform between (bboxes, lables) <-> SSD output

        dboxes: default boxes in size 8732 x 4,
            encoder: input ltrb format, output xywh format
            decoder: input xywh format, output ltrb format

        encode:
            input  : bboxes_in (Tensor nboxes x 4), labels_in (Tensor nboxes)
            output : bboxes_out (Tensor 8732 x 4), labels_out (Tensor 8732)
            criteria : IoU threshold of bboxes

        decode:
            input  : bboxes_in (Tensor 8732 x 4), scores_in (Tensor 8732 x nitems)
            output : bboxes_out (Tensor nboxes x 4), labels_out (Tensor nboxes)
            criteria : IoU threshold of bboxes
            max_output : maximum number of output bboxes
    """

    def __init__(self, dboxes):

        self.dboxes = dboxes(order="ltrb") #default boxes
        self.dboxes_xywh = dboxes(order="xywh").unsqueeze(dim=0)
        self.nboxes = self.dboxes.size(0)
        self.scale_xy = dboxes.scale_xy
        self.scale_wh = dboxes.scale_wh

    def encode(self, bboxes_in, labels_in, criteria=0.5):
        ious = box_iou(bboxes_in, self.dboxes)

        best_dbox_ious, best_dbox_idx = ious.max(dim=0) #best_dbox_idx = best gt idx for each default box
        best_bbox_ious, best_bbox_idx = ious.max(dim=1) #best_bbox_idx = best default box idx for each gt

        # set best ious 2.0
        best_dbox_ious.index_fill_(0, best_bbox_idx, 2.0) 

        num_gt = best_bbox_idx.size(0)
        idx = torch.arange(0, num_gt, dtype=torch.int64) # [0, 1, 2 ..., num_gt-1]

        matched_dboxes = best_bbox_idx[idx] # matched_dboxes == best_bbox_idx == best_bbox_idx[idx]
        
        best_dbox_idx[matched_dboxes] = idx #change the gt idx if necessary for best default_boxes

        # filter IoU > 0.5
        masks = best_dbox_ious > criteria 
        labels_out = torch.zeros(self.nboxes, dtype=torch.long)
        labels_out[masks] = labels_in[best_dbox_idx[masks]] #size: n_default_boxes, 1 if db matches a gt else 0
        
        bboxes_out = self.dboxes.clone()

        bboxes_out[masks, :] = bboxes_in[best_dbox_idx[masks], :]

        bboxes_out = box_convert(bboxes_out, in_fmt="xyxy", out_fmt="cxcywh")
        return bboxes_out, labels_out

    def scale_back_batch(self, bboxes_in, scores_in):
        """
            Do scale and transform from xywh to ltrb
            suppose input Nx4xnum_bbox Nxlabel_numxnum_bbox
        """
        if bboxes_in.device == torch.device("cpu"):
            self.dboxes = self.dboxes.cpu()
            self.dboxes_xywh = self.dboxes_xywh.cpu()
        else:
            self.dboxes = self.dboxes.cuda()
            self.dboxes_xywh = self.dboxes_xywh.cuda()

        bboxes_in = bboxes_in.permute(0, 2, 1)
        scores_in = scores_in.permute(0, 2, 1)

        bboxes_in[:, :, :2] = self.scale_xy * bboxes_in[:, :, :2]
        bboxes_in[:, :, 2:] = self.scale_wh * bboxes_in[:, :, 2:]

        bboxes_in[:, :, :2] = bboxes_in[:, :, :2] * self.dboxes_xywh[:, :, 2:] + self.dboxes_xywh[:, :, :2] # x = dboxes_x + dboxes_w*x
        bboxes_in[:, :, 2:] = bboxes_in[:, :, 2:].exp() * self.dboxes_xywh[:, :, 2:] # w = dboxes_w*exp(w)
        bboxes_in = box_convert(bboxes_in, in_fmt="cxcywh", out_fmt="xyxy")

        return bboxes_in, F.softmax(scores_in, dim=-1)

    def decode_batch(self, bboxes_in, scores_in, nms_threshold=0.45, max_output=200):
        bboxes, probs = self.scale_back_batch(bboxes_in, scores_in)
        output = []
        for bbox, prob in zip(bboxes.split(1, 0), probs.split(1, 0)): #split for batches
            bbox = bbox.squeeze(0)
            prob = prob.squeeze(0)
            output.append(self.decode_single(bbox, prob, nms_threshold, max_output)[:3])
        return output
    
    def get_matched_idx(self, bboxes_in, scores_in, nms_threshold=0.45, max_output=200):
        bboxes, probs = self.scale_back_batch(bboxes_in, scores_in)
        output = []
        for bbox, prob in zip(bboxes.split(1, 0), probs.split(1, 0)): #split for batches
            bbox = bbox.squeeze(0)
            prob = prob.squeeze(0)
            output.append(self.decode_single(bbox, prob, nms_threshold, max_output)[:])
        return output
    
    # perform non-maximum suppression
    def decode_single(self, bboxes_in, scores_in, nms_threshold, max_output, max_num=200):
        bboxes_out = []
        scores_out = []
        labels_out = []

        for i, score in enumerate(scores_in.split(1, 1)): #split for different classes
            if i == 0:
                continue #ignore background

            score = score.squeeze(1)
            mask = score > 0.05

            bboxes, score = bboxes_in[mask, :], score[mask]
            if score.size(0) == 0: continue

            score_sorted, score_idx_sorted = score.sort(dim=0)

            # select max_output indices
            score_idx_sorted = score_idx_sorted[-max_num:]
            candidates = []

            while score_idx_sorted.numel() > 0:
                idx = score_idx_sorted[-1].item()
                bboxes_sorted = bboxes[score_idx_sorted, :]
                bboxes_idx = bboxes[idx, :].unsqueeze(dim=0)
                iou_sorted = box_iou(bboxes_sorted, bboxes_idx).squeeze()
                # we only need iou < nms_threshold
                score_idx_sorted = score_idx_sorted[iou_sorted < nms_threshold]
                candidates.append(idx)

            bboxes_out.append(bboxes[candidates, :])
            scores_out.append(score[candidates])
            labels_out.extend([i] * len(candidates))

        if not bboxes_out:
            return [torch.tensor([]) for _ in range(3)]

        bboxes_out, labels_out, scores_out = torch.cat(bboxes_out, dim=0), \
                                             torch.tensor(labels_out, dtype=torch.long), \
                                             torch.cat(scores_out, dim=0)
        _, max_ids = scores_out.sort(dim=0)
        max_ids = max_ids[-max_output:] 
        return bboxes_out[max_ids, :], labels_out[max_ids], scores_out[max_ids], torch.tensor(candidates)[max_ids]


class DefaultBoxes(object):
    def __init__(self, fig_size, feat_size, steps, scales, aspect_ratios, model, scale_xy=0.1, scale_wh=0.2, debug = False):

        self.feat_size = feat_size
        self.fig_size = fig_size
        self.model = model

        self.scale_xy = scale_xy
        self.scale_wh = scale_wh

        self.steps = steps
        self.scales = scales

        fk = fig_size / np.array(steps) #38, 19, 10, 5, 3, 1
        self.aspect_ratios = aspect_ratios

        self.default_boxes = []
        if debug:
            self.debug_boxes = []
            self.debug_idx = []
            self.debug_centers = []

        for idx, sfeat in enumerate(self.feat_size):
            debug_boxes = []
            debug_idx = []
            debug_centers = []

            sk1 = scales[idx] / fig_size
            if model == "SSD":
                sk2 = scales[idx + 1] / fig_size
                sk3 = sqrt(sk1 * sk2)
                all_sizes = [(sk1, sk1), (sk3, sk3)] #aspect ratio 1 + extra box with scale = sqrt(scale*scale at next level) and aspect ratio = 1

                for alpha in aspect_ratios[idx]:
                    w, h = sk1 * sqrt(alpha), sk1 / sqrt(alpha)
                    all_sizes.append((w, h))
                    all_sizes.append((h, w))

                for k, (w, h) in enumerate(all_sizes):
                    for i, j in itertools.product(range(sfeat), repeat=2):
                        cx, cy = (j + 0.5) / fk[idx], (i + 0.5) / fk[idx]
                        self.default_boxes.append((cx, cy, w, h))
                        if debug:
                            debug_boxes.append((cx, cy, w, h))
                            debug_idx.append(i*sfeat + j)
                            debug_centers.append((cx,cy))

            elif model == "SSD_custom":
                sk2 = scales[idx + 1] / fig_size
                sk3 = sqrt(sk1 * sk2)
                all_sizes = [(sk1, sk1), (sk3, sk3)] #aspect ratio 1 + extra box with scale = sqrt(scale*scale at next level) and aspect ratio = 1

                for alpha in aspect_ratios[idx]:
                    w, h = sk1 * sqrt(alpha), sk1 / sqrt(alpha)
                    all_sizes.append((w, h)) #keep only horizontal boxes

                for k, (w, h) in enumerate(all_sizes):
                    for i, j in itertools.product(range(sfeat), repeat=2):
                        cx, cy = (j + 0.5) / fk[idx], (i + 0.5) / fk[idx]
                        self.default_boxes.append((cx, cy, w, h))
                        if debug:
                            debug_boxes.append((cx, cy, w, h))
                            debug_idx.append(i*sfeat + j)
                            debug_centers.append((cx,cy))

            elif model == "TB": #original TB implementation	
                all_sizes = [(sk1, sk1)]

                for alpha in aspect_ratios[idx]:
                    w, h = sk1 * sqrt(alpha), sk1 / sqrt(alpha)
                    all_sizes.append((w, h))

                for k, (w, h) in enumerate(all_sizes):
                    for i, j in itertools.product(range(sfeat), repeat=2):
                        cx, cy = (j + 0.5) / fk[idx], (i + 0.5) / fk[idx]
                        cy_offset = (i + 1) / fk[idx]
                        self.default_boxes.append((cx, cy, w, h))
                        self.default_boxes.append((cx, cy_offset, w, h))
                        if debug:
                            debug_boxes.append((cx, cy, w, h))
                            debug_boxes.append((cx, cy_offset, w, h))
                            debug_idx.append(i*sfeat + j)
                            debug_idx.append(i*sfeat + j)
                            debug_centers.append((cx,cy))
                        
                        
            elif model == "TB_noOffset":
                all_sizes = [(sk1, sk1)]

                sk2 = scales[idx + 1] / fig_size
                sk3 = sqrt(sk1 * sk2)
                all_sizes.append((sk3,sk3))
                
                for alpha in aspect_ratios[idx]:
                    w, h = sk1 * sqrt(alpha), sk1 / sqrt(alpha)
                    all_sizes.append((w, h))

                for k, (w, h) in enumerate(all_sizes):
                    for i, j in itertools.product(range(sfeat), repeat=2):
                        cx, cy = (j + 0.5) / fk[idx], (i + 0.5) / fk[idx]
                        self.default_boxes.append((cx, cy, w, h))

                        if debug:
                            debug_boxes.append((cx, cy, w, h))
                            debug_idx.append(i*sfeat + j)
                            debug_centers.append((cx,cy))
                        

            if debug:
                debug_boxes = torch.tensor(debug_boxes, dtype=torch.float)
                debug_boxes.clamp_(min=0, max=1)
                self.debug_boxes.append(debug_boxes)
                self.debug_idx.append(debug_idx)
                self.debug_centers.append(debug_centers)

        self.dboxes = torch.tensor(self.default_boxes, dtype=torch.float)
        self.dboxes.clamp_(min=0, max=1)
        self.dboxes_ltrb = box_convert(self.dboxes, in_fmt="cxcywh", out_fmt="xyxy")

    def __call__(self, order="ltrb"):
        if order == "ltrb":
            return self.dboxes_ltrb
        else:  # order == "xywh"
            return self.dboxes


def generate_dboxes(model, trunc, figsize, debug = False):
    if model == "SSD" or model == "SSD_custom":
        if figsize == 300:
            if trunc:
                feat_size = [38, 19, 10]
                steps = [8, 16, 32] #300/8 = 38, 300/16 = 19, 300/32 = 10, 300/64 = 5, 300/100 = 3, 300/300 = 1
                scales = [21, 45, 99, 153] #21/300 = 0.07, 45/300 = 0.15, 99/300 = 0.33, 153/300 = 0.51, 207/300 = 0.69, , 261/300 = 0.87, 315/300 = 1.05
                aspect_ratios = [[2], [2, 3], [2, 3]]
            else:
                feat_size = [38, 19, 10, 5, 3, 1]
                steps = [8, 16, 32, 64, 100, 300] #300/8 = 38, 300/16 = 19, 300/32 = 10, 300/64 = 5, 300/100 = 3, 300/300 = 1
                scales = [21, 45, 99, 153, 207, 261, 315] #21/300 = 0.07, 45/300 = 0.15, 99/300 = 0.33, 153/300 = 0.51, 207/300 = 0.69, , 261/300 = 0.87, 315/300 = 1.05
                aspect_ratios = [[2], [2, 3], [2, 3], [2, 3], [2], [2]]
        else:
            if trunc:
                feat_size = [64, 32, 16]
                steps = [8, 16, 32]
                scales = [21, 52, 133, 215] #21/512 = 0.04, 52/512 = 0.1, 133/512 = 0.26, 215/512 = 0.42, 297/512 = 0.58, , 379/512 = 0.74, 460/512 = 0.89, 537/512 = 1.05
                aspect_ratios = aspect_ratios = [[2], [2, 3], [2, 3]]
            else:
                feat_size = [64, 32, 16, 8, 4, 2, 1]
                steps = [8, 16, 32, 64, 128, 256, 512]
                scales = [21, 52, 133, 215, 297, 379, 460, 537] #21/512 = 0.04, 52/512 = 0.1, 133/512 = 0.26, 215/512 = 0.42, 297/512 = 0.58, , 379/512 = 0.74, 460/512 = 0.89, 537/512 = 1.05
                aspect_ratios = aspect_ratios = [[2], [2, 3], [2, 3], [2, 3], [2], [2], [2]]
    elif model == "TB_noOffset" or model == "TB":
        if figsize == 300:
            if trunc:
                feat_size = [38, 19, 10]
                steps = [8, 16, 32]
                scales = [21, 45, 99, 153]
                aspect_ratios = [[2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10]]
            else:
                feat_size = [38, 19, 10, 5, 3, 1]
                steps = [8, 16, 32, 64, 100, 300]
                scales = [21, 45, 99, 153, 207, 261, 315]
                aspect_ratios = [[2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10]]
        else:
            if trunc:
                feat_size = [64, 32, 16]
                steps = [8, 16, 32]
                scales = [21, 52, 133,215] #21/512 = 0.04, 52/512 = 0.1, 133/512 = 0.26
                aspect_ratios = [[2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10]]
            else:
                feat_size = [64, 32, 16, 8, 4, 2, 1]
                steps = [8, 16, 32, 64, 128, 256, 512]
                scales = [21, 52, 133, 215, 297, 379, 460, 537] #21/512 = 0.04, 52/512 = 0.1, 133/512 = 0.26, 215/512 = 0.42, 297/512 = 0.58, , 379/512 = 0.74, 460/512 = 0.89, 537/512 = 1.05
                aspect_ratios = [[2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10], [2,3,5,7,10]]
    return DefaultBoxes(figsize, feat_size, steps, scales, aspect_ratios, model, debug = debug)
