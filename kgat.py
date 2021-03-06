import argparse
from dataset import DataLoader
from models import Model
import torch as th
import torch.optim as optim
import metric
from utils import creat_log_id, logging_config, MetricLogger
from time import time
import os
import logging

def parse_args():
    parser = argparse.ArgumentParser(description="Reproduce KGAT using DGL")
    parser.add_argument('--gpu', type=int, default=0, help='use GPU')
    parser.add_argument('--seed', type=int, default=1234, help='the random seed')
    ### Data parameters
    parser.add_argument('--data_name', nargs='?', default='yelp2018',
                        help='Choose a dataset from {yelp2018, amazon-book}')
    #parser.add_argument('--adj_type', nargs='?', default='si', help='Specify the type of the adjacency (laplacian) matrix from {bi, si}.')
    ### Model parameters
    parser.add_argument('--entity_embed_dim', type=int, default=64, help='KG entity Embedding size.')
    parser.add_argument('--relation_embed_dim', type=int, default=64, help='KG relation Embedding size.')
    parser.add_argument('--gnn_model', type=str, default="kgat", help='the gnn models')
    parser.add_argument('--gnn_num_layer', type=int, default=3, help='the number of layers')
    parser.add_argument('--gnn_hidden_size', type=int, default=64, help='Output sizes of every layer')
    parser.add_argument('--dropout_rate', type=float, default=0.1, help='Keep probability w.r.t. node dropout (i.e., 1-dropout_ratio) for each deep layer. 1: no dropout.')
    parser.add_argument('--use_attention', type=bool, default=True, help='Whether to use attention to update adj')
    parser.add_argument('--regs', type=float, default=0.0001, help='Regularization for user and item embeddings.')

    ### Training parameters
    parser.add_argument('--max_epoch', type=int, default=5000, help='train xx iterations')
    parser.add_argument("--grad_norm", type=float, default=1.0, help="norm to clip gradient to")
    parser.add_argument('--lr', type=float, default=0.001, help='Learning rate.')
    parser.add_argument('--batch_size', type=int, default=10240, help='CF batch size.')
    parser.add_argument('--batch_size_kg', type=int, default=2048, help='KG batch size.')
    parser.add_argument('--joint_train', type=bool, default=False, help='Whether to jointly-train the mode or '
                                                                        'alternative train the model ')
    parser.add_argument('--evaluate_every', type=int, default=1, help='the evaluation duration')
    parser.add_argument('--print_every', type=int, default=200, help='the print duration')
    #parser.add_argument("--eval_batch_size", type=int, default=-1, help="batch size when evaluating")
    args = parser.parse_args()
    save_dir = "{}_d{}_l{}_dp{}_lr{}_bz{}_kgbz{}_att{}_seed{}".format(args.data_name,
                args.entity_embed_dim, args.gnn_num_layer, args.dropout_rate, args.lr,
                args.batch_size, args.batch_size_kg, int(args.use_attention), args.seed)
    args.save_dir = os.path.join('log', save_dir)
    if not os.path.isdir('log'):
        os.makedirs('log')
    if not os.path.isdir(args.save_dir):
        os.makedirs(args.save_dir)
    args.save_id = creat_log_id(args.save_dir)
    return args

def eval(model, g, train_user_dict, eval_user_dict, item_id_range, use_cuda, use_attention):
    with th.no_grad():
        if use_attention:
            print("Compute attention weight in eval func ...")
            A_w = model.compute_attention(g)
            g.edata['w'] = A_w
        all_embedding = model.gnn(g, g.ndata['id'])
        recall, ndcg = metric.calc_recall_ndcg(all_embedding, train_user_dict, eval_user_dict,
                                               item_id_range, K=20, use_cuda=use_cuda)
    return recall, ndcg

def train_eval(args):
    logging_config(folder=args.save_dir, name='log{:d}'.format(args.save_id), no_console=False)
    logging.info(args)

    ### check context
    use_cuda = args.gpu >= 0 and th.cuda.is_available()
    if use_cuda:
        th.cuda.set_device(args.gpu)

    ### load data
    dataset = DataLoader(data_name=args.data_name, seed=args.seed)
    print(dataset)
    model = Model(use_KG=True, input_node_dim=args.entity_embed_dim, gnn_model=args.gnn_model,
                  num_gnn_layers=args.gnn_num_layer, n_hidden=args.gnn_hidden_size, dropout=args.dropout_rate,
                  n_entities=dataset.n_KG_entity, n_relations=dataset.n_KG_relation,
                  relation_dim=args.relation_embed_dim,
                  reg_lambda_kg=args.regs, reg_lambda_gnn=args.regs)
    if use_cuda:
        model.cuda()
    logging.info(model)
    ### optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    valid_metric_logger = MetricLogger(['epoch', 'recall', 'ndcg', 'is_best'],
                                       ['%d', '%.5f', '%.5f', '%d'],
                                       os.path.join(args.save_dir, 'valid{:d}.csv'.format(args.save_id)))
    test_metric_logger = MetricLogger(['epoch', 'recall', 'ndcg'],
                                       ['%d', '%.5f', '%.5f'],
                                       os.path.join(args.save_dir, 'test{:d}.csv'.format(args.save_id)))
    best_epoch = -1
    best_recall = 0.0

    train_g = dataset.train_g
    nid_th = th.LongTensor(train_g.ndata["id"])
    etype_th = th.LongTensor(train_g.edata["type"])
    if use_cuda:
        nid_th, etype_th = nid_th.cuda(), etype_th.cuda()
    train_g.ndata['id'] = nid_th
    train_g.edata['type'] = etype_th

    test_g = dataset.test_g
    nid_th = th.LongTensor(test_g.ndata["id"])
    etype_th = th.LongTensor(test_g.edata["type"])
    if use_cuda:
        nid_th, etype_th = nid_th.cuda(), etype_th.cuda()
    test_g.ndata['id'] = nid_th
    test_g.edata['type'] = etype_th

    item_id_range = th.LongTensor(dataset.item_id_range).cuda() if use_cuda \
        else th.LongTensor(dataset.item_id_range)

    for epoch in range(1, args.max_epoch+1):
        ### train kg
        time1 = time()
        kg_sampler = dataset.KG_sampler(batch_size=args.batch_size_kg)
        iter = 0
        total_loss = 0.0
        for h, r, pos_t, neg_t, _ in kg_sampler:
            iter += 1
            model.train()
            h_th = th.LongTensor(h)
            r_th = th.LongTensor(r)
            pos_t_th = th.LongTensor(pos_t)
            neg_t_th = th.LongTensor(neg_t)
            if use_cuda:
                h_th, r_th, pos_t_th, neg_t_th = h_th.cuda(), r_th.cuda(), pos_t_th.cuda(), neg_t_th.cuda()
            loss = model.transR(h_th, r_th, pos_t_th, neg_t_th)
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
            total_loss += loss.item()
            if (iter % args.print_every) == 0 or iter == 1:
                logging.info("Epoch {:04d} Iter {:04d} | Loss {:.4f} ".format(epoch, iter, total_loss/iter))
        logging.info('Time for KGE: {:.1f}s, loss {:.4f}'.format(time() - time1, total_loss/iter))

        ### train GNN
        if args.use_attention:
            time1 = time()
            print("Compute attention weight in train ...")
            with th.no_grad():
                A_w = model.compute_attention(train_g)
            train_g.edata['w'] = A_w
            print("Time: {:.2f}s".format(time() - time1))
        time1 = time()
        cf_sampler = dataset.CF_pair_sampler(batch_size=args.batch_size)
        iter = 0
        total_loss = 0.0
        for user_ids, item_pos_ids, item_neg_ids, _ in cf_sampler:
            iter += 1
            model.train()
            user_ids_th = th.LongTensor(user_ids)
            item_pos_ids_th = th.LongTensor(item_pos_ids)
            item_neg_ids_th = th.LongTensor(item_neg_ids)
            if use_cuda:
                user_ids_th, item_pos_ids_th, item_neg_ids_th = \
                    user_ids_th.cuda(), item_pos_ids_th.cuda(), item_neg_ids_th.cuda()
            embedding = model.gnn(train_g, train_g.ndata['id'])
            loss = model.get_loss(embedding, user_ids_th, item_pos_ids_th, item_neg_ids_th)
            loss.backward()
            # th.nn.utils.clip_grad_norm_(model.parameters(), args.grad_norm)  # clip gradients
            optimizer.step()
            optimizer.zero_grad()
            total_loss += loss.item()
            if (iter % args.print_every) == 0 or iter == 1:
                logging.info("Epoch {:04d} Iter {:04d} | Loss {:.4f} ".format(epoch, iter, total_loss / iter))
        logging.info('Time for GNN: {:.1f}s, loss {:.4f}'.format(time() - time1, total_loss / iter))


        if epoch % args.evaluate_every == 0:
            time1 = time()
            val_recall, val_ndcg = eval(model, train_g, dataset.train_user_dict, dataset.valid_user_dict,
                                        item_id_range, use_cuda, args.use_attention)

            info = "Epoch{}, [{:.1f}s] val recall:{:.5f}, val ndcg:{:.5f}".format(
                    epoch, time()-time1, val_recall, val_ndcg)
            # save best model
            if val_recall > best_recall:
                valid_metric_logger.log(epoch=epoch, recall=val_recall, ndcg=val_ndcg, is_best=1)
                best_recall = val_recall
                #best_ndcg = val_ndcg
                best_epoch = epoch
                time1 = time()
                test_recall, test_ndcg = eval(model, test_g, dataset.train_valid_user_dict, dataset.test_user_dict,
                                              item_id_range, use_cuda, args.use_attention)
                test_metric_logger.log(epoch=epoch, recall=test_recall, ndcg=test_ndcg)

                info += "\t[{:.1f}s] test recall:{:.5f}, test ndcg:{:.5f}".format(time() - time1, test_recall, test_ndcg)
                #th.save({'state_dict': model.state_dict(), 'epoch': epoch}, model_state_file)
            else:
                valid_metric_logger.log(epoch=epoch, recall=val_recall, ndcg=val_ndcg, is_best=0)
                recall, ndcg = eval(model, test_g, dataset.train_valid_user_dict, dataset.test_user_dict,
                                              item_id_range, use_cuda, args.use_attention)
                print("test recall:{}, test_ndcg: {}".format(recall, ndcg))
            logging.info(info)

    logging.info("Final test recall:{:.5f}, test ndcg:{:.5f}, best epoch:{}".format(test_recall, test_ndcg, best_epoch))

if __name__ == '__main__':
    args = parse_args()
    train_eval(args)