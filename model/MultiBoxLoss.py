# coding:utf-8

import torch
import torch.nn as nn
import torch.nn.functional as F


class MultiBoxLoss(nn.Module):
    def __init__(self, num_classes=21):
        super(MultiBoxLoss, self).__init__()
        self.num_classes = num_classes
        self.smooth_l1_loss = nn.SmoothL1Loss(size_average=False)  # size_average=False　指定求得的loss不除以mini-batch

    def cross_entropy_loss(self, x, y):  # Softmax Loss
        """
        x: tensor (batch_size*8732, num_classes) batch_size*8732个default box num_classes个类别
        y: tensor (batch_size*8732,) 每条数据的标签，值在0~D-1之间. 整个batch内做了扁平化处理
        return: (batch_size*8732,)
        对所有的default box进行交叉熵的计算
        """
        xmax = x.detach().max()  # 标量，为了数值运算的稳定性
        # tensor.detach() 返回一个新的 从当前图中分离的 Variable，
        # 并且返回的 Variable 永远不会需要梯度。　返回的 Variable 和 被 detach 的Variable 指向同一个 tensor
        # 这里的log其实在程序里默认的是以自然数e为底的
        log_sum_exp = torch.log(torch.sum(torch.exp(x - xmax), 1)) + xmax  # (batch_size*default_box,)
        # 这个x.gather(1, y.view(-1,1))是(1,batch_size*default_box) 直接和log_sum_exp相减会
        # 进行广播到(batch_size*default_box, batch_size*default_box), 所以需要进行squeeze()  !!!
        return log_sum_exp - x.gather(1, y.view(-1, 1)).squeeze()  # 为了便于理解，可以把上面的xmax放进负号里

    def hard_negative_mining(self, conf_loss, pos):
        """
        conf_loss: tensor (batch_size*8732) 先用非背景类计算loss
        pos: tensor (batch_size, 8732) boolean标签,default box中和label box进行match得到的匹配的框，
        每个框的匹配程度iou. N个图片，每个图片都有8732个default box
        return: neg tensor,(N, 8732) boolean矩阵，为1表示是选出来的negative box
        计算过全部default box的交叉熵之后在从负样本中选出3倍正样本的数目，然后选出来的这些pos neg的交叉熵再进行反向传播
        """
        batch_size, num_boxes = pos.size()
        # print('pos:', pos.size())
        # print(pos)
        # print('conf_loss:', conf_loss.size())
        # print(conf_loss)
        # 将正例置为0，剩下的就是negative box，下面就是非背景类的conf_loss置0，为了后面对背景类排序筛选
        # 这里pos是(batch_size, 8732) 所以需要view(-1) !!
        conf_loss[pos.view(-1)] = 0
        # (N, 8732) N个图片，每个图片都有8732个default box 这里是每个图片 每个default box的loss
        conf_loss = conf_loss.view(batch_size, -1)

        # 对分类的loss进行降序排序，每个图片内部进行排序，所以sort中的dim=1
        # _ 是排好序的conf_loss:(batch_size, 8732),每个图片内部的8732个default box排好序了
        # idx是排好序的下标(batch_size, 8732)，分行进行索引的，每行是一个图片（样本）所有default box的confidence值
        _, idx = conf_loss.sort(dim=1, descending=True)
        # print(idx)
        # rank(batch_size, 8732) 
        _, rank = idx.sort(dim=1)  # 默认升序排序
        # print(rank)

        # (batch_size,) 每个图片中正例的个数
        num_pos = pos.long().sum(1)  # #### 64-bit integer (signed):	torch.int64 or torch.long  ###
        # 每张图片中的正例个数乘以3得到每张图片中hard mining的结果的负样本数,但是###### 最大不能超过8731,这样不会有重复样本出现######
        num_neg = torch.clamp(3 * num_pos, max=num_boxes - 1)  # （batch_size,）

        # print(num_neg.size())
        # print(rank.size())
        num_neg = num_neg.unsqueeze(1)
        neg = rank < num_neg.expand_as(rank)  # 先插入一个轴，然后复制成rank的shape大小。也可以不expand，直接广播
        # 比如在当前某位置上的conf_loss是第30大的（对应rank_ij=30)，而num_neg=6,则此位置是False,即不选入
        # print('neg', neg.size())
        return neg

    def forward(self, loc_preds, loc_targets, conf_preds, conf_targets):
        """
        loc_preds: tensor 预测的box位置 (batch_size, 8732, 4)
        loc_targets: tensor (batch_size, 8732, 4) 
                     deafult box和label box进行匹配之后进行计算iou，然后将每个default box的值赋成iou最大的那个值
                     经过prior box中的match之后从(xmin, ymin, xmax, ymax)转换成(cx, cy, h, w)格式的标签
                     iou<0.5的都变成了0背景类
        conf_preds: tensor 预测的概率 (batch_size, 8732, num_classes)
        conf_targets: tensor (batch_size, 8732) 经过prior box中的match之后每个default box对应的target
        """
        batch_size, num_boxes, _ = loc_preds.size()

        pos = conf_targets > 0  # 非背景类,除了hard mining出来的背景 其余不参与 计算loss (batch_size, 8732)
        # 所有非背景的box数目
        num_matched_boxes = pos.detach().sum()  # detach之后将不计算梯度
        # print('in multiboxloss forward() num_matched_boxes : ', num_matched_boxes.sum())
        # 如果预测出来全是背景，则loss为0
        if num_matched_boxes == 0:
            return torch.zeros((1), requires_grad=True)  # 若前面有一个输入需要梯度，则后面的输出也需要梯度。有的版本这里是默认值false
        # 注：　Tensor变量的requires_grad的属性默认为False,若一个节点requires_grad被设置为True，那么所有依赖它的节点的requires_grad都为True。

        # 计算localization loss = SmoothL1Loss(pos_loc_preds, pos_loc_targets)，只计算前景框的损失，背景类没有框
        # 将非背景的box的下标扩展到(batch_size, 8732, 4)
        # pos和conf_targets的shape是(batch_size, 8732)
        pos_mask = pos.unsqueeze(2).expand_as(loc_preds)  # pos先变成（batch_size, 8732, 1)然后复制成（batch_size, 8732, 4)
        # 先筛选出非背景的box，然后再resize(box_num, 4)
        pos_loc_preds = loc_preds[pos_mask].view(-1, 4)  # 变成(box_num, 4)的原因是想使用torch内的smooth_L1_loss
        # 先筛选出非背景的box的类别，然后resize(box_num, 4)
        pos_loc_targets = loc_targets[pos_mask].view(-1, 4)
        loc_loss = self.smooth_l1_loss(pos_loc_preds, pos_loc_targets)  # 这里求的是loss的和，并没有做平均

        # 计算confidence loss = CrossEntropyLoss(pos_conf_preds, pos_conf_targets)
        #                      +CrossEntropyLoss(neg_conf_preds, neg_conf_targets)
        # (batch_size*8732,) 这里是对所有的default box计算交叉熵，但是其中包含了太多的背景类，不适合作为反向传播的依据，所以再进行hard mining
        # conf_preds 原来是(batch_size, 8732, num_classes)  conf_targets 原来是(batch_size, 8732)，　使用的自定义的cross entropy loss
        conf_loss = self.cross_entropy_loss(conf_preds.view(-1, self.num_classes), conf_targets.view(-1))  # 用于难样本挖掘
        # why not use conf_loss = F.cross_entropy(conf_preds.view(-1, self.num_classes),
        # conf_targets.view(-1), reduce=False)  # 用于难样本挖掘
        # 已经解决: 为什么要使用自己实现的交叉熵函数，而不是用现成的？　这是因为旧版本的pytorch不支持element-wise返回
        # print('conf_loss', conf_loss.size())
        # mining出来的neg box (batch_size, 8732) 大于零的位置表示是mining出来的
        neg = self.hard_negative_mining(conf_loss, pos)
        # pos_mask (batch_size, 8732, num_classes) 大于零的是非背景类
        pos_mask = pos.unsqueeze(2).expand_as(conf_preds)
        # neg_mask (batch_size, 8732, num_classes) 大于零的是mining出来的负样本
        neg_mask = neg.unsqueeze(2).expand_as(conf_preds)
        # 构造mask mask中大于零的是选出来进行计算分类loss的 gt： greater than
        mask = (pos_mask + neg_mask).gt(0)

        pos_and_neg = (pos + neg).gt(0)  # (batch_size, 8732) 大于零的是选出来的pos以及neg
        preds = conf_preds[mask].view(-1, self.num_classes)
        targets = conf_targets[pos_and_neg]
        conf_loss = F.cross_entropy(preds, targets, size_average=False)
        # print('the conf_loss is: ', conf_loss)

        # 因为loc_loss是float32，而num_matched_box是int64，没办法直接除所以转换一下
        # 这里是不会损失数据的，因为假如batch_size=32,每个图片8732个，就只有8732*32=279424
        # num_matched_boxes最大的值不会超过float32的表示范围的
        num_matched_boxes = num_matched_boxes.to(torch.float32)  # Tensor dtype and/or device 转换
        loc_loss /= num_matched_boxes   # 除以的是正样本的数目
        conf_loss /= num_matched_boxes
        # print('  average loc_loss: %f, average conf_loss: %f'% (loc_loss.item(), conf_loss.item()))
        return loc_loss + conf_loss

    def test_cross_entropy_loss(self):
        a = torch.randn(10, 4)
        b = torch.ones(10).long()
        loss = self.cross_entropy_loss(a, b)
        print(loss.mean())
        print(F.cross_entropy(a, b))


def main():
    # 固定随机种子
    SEED = 0
    torch.manual_seed(SEED)
    torch.cuda.manual_seed(SEED)

    batch_size = 2
    num = 3
    num_classes = 4
    loss_func = MultiBoxLoss(num_classes)

    loc_preds = torch.rand((batch_size, num, 4), requires_grad=True)
    loc_targets = torch.rand((batch_size, num, 4))
    conf_preds = torch.rand((batch_size, num, num_classes), requires_grad=True)
    conf_targets = torch.tensor([[0, 1, 2], [2, 1, 0]], dtype=torch.long)
    z = loss_func(loc_preds, loc_targets, conf_preds, conf_targets)
    # 看一下自己实现的和torch里的交叉熵函数是否一致
    loss_func.test_cross_entropy_loss()
    print(z, z.requires_grad)


def cross_entropy_loss():
    """
    首先这个函数计算的交叉熵是正确的，但是这个的输出是(batch_size*default_box, batch_size*default_box)
    我要改正把他变成输出成(batch_size*default_box,)
    
    x: tensor (batch_size*8732, num_classes) batch_size*8732个default box num_classes个类别
    y: tensor (batch_size*8732,) 每条数据的标签，值在0~D-1之间
    return: (batch_size*8732,)
    对所有的default box进行交叉熵的计算
    """
    batch_size = 2
    default_box = 3
    num_classes = 4
    x = torch.rand((batch_size * default_box, num_classes))
    y = torch.tensor([0, 1, 2, 3, 2, 3])
    xmax = x.detach().max()
    print(xmax)
    print(xmax.size())
    log_sum_exp = torch.log(torch.sum(torch.exp(x - xmax), 1)) + xmax
    print(log_sum_exp.size())
    print(log_sum_exp)
    print(x.gather(1, y.view(-1, 1)).squeeze())
    z = log_sum_exp - x.gather(1, y.view(-1, 1)).squeeze()
    print(z)


def testc():
    loss = MultiBoxLoss()
    loss.test_cross_entropy_loss()


if __name__ == '__main__':
    main()
