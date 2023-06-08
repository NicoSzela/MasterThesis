"""
@author: Viet Nguyen (nhviet1009@gmail.com)
"""
import torch
from torchvision.datasets import CocoDetection
from torch.utils.data.dataloader import default_collate
import os
import matplotlib.pyplot as plt
import torchvision.transforms as transforms

def collate_fn(batch):
    items = list(zip(*batch))
    items[0] = default_collate([i for i in items[0] if torch.is_tensor(i)])
    items[1] = list([i for i in items[1] if i])
    items[2] = list([i for i in items[2] if i])
    items[3] = default_collate([i for i in items[3] if torch.is_tensor(i)])
    items[4] = default_collate([i for i in items[4] if torch.is_tensor(i)])
    return items
                
class CustomDataset(CocoDetection):
    def __init__(self, root, jsonFile, image_dir, transform=None):
        annFile = os.path.join(root,"annotations", jsonFile)
        root = os.path.join(root, image_dir)
        super(CustomDataset, self).__init__(root, annFile)
        self._load_categories()
        self.transform = transform

    def _load_categories(self):
        categories = self.coco.loadCats(self.coco.getCatIds())
        categories.sort(key=lambda x: x["id"])

        self.label_map = {}
        self.label_info = {}
        counter = 1
        self.label_info[0] = "background"
        for c in categories:
            self.label_map[c["id"]] = counter
            self.label_info[counter] = c["name"]
            counter += 1

    def __getitem__(self, item, debug = False):
        image, target = super(CustomDataset, self).__getitem__(item)
        width, height = image.size
        boxes = []
        labels = []
        if len(target) == 0:
            return None, None, None, None, None
        for annotation in target:
            bbox = annotation.get("bbox")
            boxes.append([bbox[0] / width, bbox[1] / height, (bbox[0] + bbox[2]) / width, (bbox[1] + bbox[3]) / height])
            labels.append(self.label_map[annotation.get("category_id")])
        boxes = torch.tensor(boxes)
        labels = torch.tensor(labels)

        if self.transform is not None and not debug:
            image, (height, width), boxes, labels = self.transform(image, (height, width), boxes, labels)

        return image, target[0]["image_id"], (height, width), boxes, labels
    
    def __plotitem__(self, item, figsize, normalizer, show_gt = False):
        img, img_id, img_size, gloc, glabel = self.__getitem__(item, debug = True)
        filename = super(CustomDataset, self)._get_image_path(img_id)
        print(filename)

        if normalizer is not None:
            img = transforms.Compose([
                        transforms.Resize(figsize),
                        transforms.ToTensor(),
                        normalizer
                    ])(img)
        else:
            img = transforms.Compose([
                        transforms.Resize(figsize),
                        transforms.ToTensor(),
                    ])(img)
        
        fig, ax = plt.subplots(figsize=(12, 12))
        ax.imshow(img.permute(1, 2, 0))

        if show_gt:
            for box in gloc:
                xmin, ymin, xmax, ymax = box*figsize[0]
                box_width = xmax - xmin
                box_height = ymax - ymin
                coords = (xmin,ymin), box_width, box_height

                ax.add_patch(plt.Rectangle(*coords, fill=False, edgecolor='green', linewidth=1.5))


        plt.show()
        return img, img_id,filename, img_size, gloc, glabel

# class CocoDataset(CocoDetection):
#     def __init__(self, root, year, mode, transform=None):
#         annFile = os.path.join(root, "annotations", "instances_{}{}.json".format(mode, year))
#         root = os.path.join(root, "{}{}".format(mode, year))
#         super(CocoDataset, self).__init__(root, annFile)
#         self._load_categories()
#         self.transform = transform

#     def _load_categories(self):
#         print("load cat")
#         categories = self.coco.loadCats(self.coco.getCatIds())
#         print(categories)
#         categories.sort(key=lambda x: x["id"])

#         self.label_map = {}
#         self.label_info = {}
#         counter = 1
#         self.label_info[0] = "background"
#         for c in categories:
#             self.label_map[c["id"]] = counter
#             self.label_info[counter] = c["name"]
#             counter += 1

#     def __getitem__(self, item):
#         image, target = super(CocoDataset, self).__getitem__(item)
#         width, height = image.size
#         boxes = []
#         labels = []
#         if len(target) == 0:
#             return None, None, None, None, None
#         for annotation in target:
#             bbox = annotation.get("bbox")
#             boxes.append([bbox[0] / width, bbox[1] / height, (bbox[0] + bbox[2]) / width, (bbox[1] + bbox[3]) / height])
#             labels.append(self.label_map[annotation.get("category_id")])
#         boxes = torch.tensor(boxes)
#         labels = torch.tensor(labels)
#         if self.transform is not None:
#             image, (height, width), boxes, labels = self.transform(image, (height, width), boxes, labels)
#         return image, target[0]["image_id"], (height, width), boxes, labels