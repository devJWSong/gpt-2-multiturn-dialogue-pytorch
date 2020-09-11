from gru_transformer import *
from recosa_transformer import *
from custom_data import *
from tqdm import tqdm
from torch.utils.data import DataLoader
from torch.nn import functional as F

import torch
import os, sys
import numpy as np
import argparse
import sentencepiece as spm
import time
import copy


class Manager():
    def __init__(self, mode, model_type, ckpt_name=None):
        print("Setting the configurations...")
        self.config = {
            'model_type': model_type,
            'device': torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu'),
            'learning_rate': 0.00001,
            'batch_size': 5,
            'num_epochs': 10,
            'max_len': 256,
            'num_heads': 8,
            'encoder_num_layers': 6,
            'decoder_num_layers': 6,
            'd_model': 512,
            'd_mid': 768,
            'd_ff': 2048,
            'dropout': 0.1,
            'max_turn': 35,
            'nucleus_p': 0.95,
            'ckpt_dir': 'saved_models',
            'data_dir': 'data',
            'train_name': 'train',
            'valid_name': 'validation',
            'processed_dir': 'processed',
            'sp_dir': 'trained_sp',
            'sp_prefix': 'sp',
            'pad_id': 0,
            'bos_id': 1,
            'eos_id': 2,
            'unk_id': 3,
            'dialogue_split_line': '[END OF DIALOGUE]',
            'end_command': 'abort!'
        }
        self.config['gru_num_layers'] = 1 if self.config['model_type']=='gru' else 2
        self.config['hidden_size'] = 128 if self.config['model_type']=='gru' else self.config['d_model']
        self.config['gru_dropout'] = 0.0 if self.config['model_type']=='gru' else 0.3
        
        # Sentencepiece tokenizer & vocab
        self.tokenizer = spm.SentencePieceProcessor()
        self.tokenizer.Load(f"{self.config['sp_dir']}/{self.config['sp_prefix']}.model")
        with open(f"{self.config['sp_dir']}/{self.config['sp_prefix']}.vocab", 'r') as f:
            lines = f.readlines()
        self.config['vocab_size'] = len(lines)
        
        # Load model & optimizer      
        print("Loading the model and optimizer...")
        if self.config['model_type'] == 'gru':
            self.model = GRUTransformer(self.config).to(self.config['device'])
        elif self.config['model_type'] == 'recosa':
            self.model = ReCoSaTransformer(self.config).to(self.config['device'])
        self.optim = torch.optim.Adam(self.model.parameters(), lr=self.config['learning_rate'])
        self.best_loss = sys.float_info.max
        
        if not os.path.exists(self.config['ckpt_dir']):
            os.mkdir(self.config['ckpt_dir'])
        
        if ckpt_name is not None:
            assert os.path.exists(f"{self.config['ckpt_dir']}/{ckpt_name}"), f"There is no checkpoint named {ckpt_name}."

            print("Loading checkpoint...")
            checkpoint = torch.load(f"{self.config['ckpt_dir']}/{ckpt_name}")
            self.model.load_state_dict(checkpoint['model_state_dict'])
            self.optim.load_state_dict(checkpoint['optim_state_dict'])
            self.best_loss = checkpoint['loss']
        else:
            print("Initializing the model...")
            self.model.init_model()
              
        if mode == 'train':
            # Load loss function
            print("Loading loss function...")
            self.criterion = nn.NLLLoss()
            
            # Load train & valid dataset
            print("Loading train & valid data...")
            train_set = CustomDataset('train', self.config)
            valid_set = CustomDataset('valid', self.config)
            self.train_loader = DataLoader(train_set, shuffle=True, batch_size=self.config['batch_size'])
            self.valid_loader = DataLoader(valid_set, shuffle=True, batch_size=self.config['batch_size'])
              
        print("Setting finished.")
              
    def train(self):
        print("Training starts.")
              
        for epoch in range(1, self.config['num_epochs']+1):
            self.model.train()
            
            print(f"#################### Epoch: {epoch} ####################")
            train_losses = []  
            for i, batch in enumerate(tqdm(self.train_loader)):
                src_inputs, trg_inputs, trg_outputs = batch[:, :, 0], batch[:, :, 1], batch[:, :, 2]  # (B, T, L)
              
                dialogue_losses = []
                
                # Only used for context GRU.
                context = torch.zeros(src_inputs.shape[0], self.config['hidden_size']).to(self.config['device'])
                # Only used for ReCoSa.
                hists = torch.zeros(self.config['max_turn'], src_inputs.shape[0], self.config['d_model']).to(self.config['device'])
                for t in range(self.config['max_turn']):
                    if t < self.config['max_turn']-1:
                        src_input, trg_input, trg_output = \
                            src_inputs[:, t].to(self.config['device']), \
                            trg_inputs[:, t].to(self.config['device']), \
                            trg_outputs[: ,t].to(self.config['device'])  # (B, L)
                    
                        if self.config['model_type'] == 'gru':                              
                            output, context = self.model(src_input, trg_input, context)  # (B, L, vocab_size), (B, d_h)
                            
                        elif self.config['model_type'] == 'recosa':
                            output, hists = self.model(src_input, trg_input, hists, num_turn=t)  # (B, L, vocab_size), (T, B, d_model)
                        
                        self.optim.zero_grad()
              
                        loss = self.criterion(
                            output.view(-1, self.config['vocab_size']),
                            trg_output.contiguous().view(output.shape[0] * output.shape[1])
                        )
                  
                        loss.backward(retain_graph=True)
                        self.optim.step()
              
                        dialogue_losses.append(loss.item())
                
                        del src_input, trg_input, trg_output
                        torch.cuda.empty_cache()
                
                train_losses += dialogue_losses
              
            mean_train_loss = np.mean(train_losses)
            print(f"Train loss: {mean_train_loss}")
            
            valid_loss = self.validation()
              
            if valid_loss < self.best_loss:
                state_dict = {
                    'model_state_dict': self.model.state_dict(),
                    'optim_state_dict': self.optim.state_dict(),
                    'loss': self.best_loss
                }
              
                torch.save(state_dict, f"{self.config['ckpt_dir']}/best_ckpt.tar")
                print(f"***** Current best checkpoint is saved. *****")
                self.best_loss = valid_loss
              
            print(f"Best valid loss: {self.best_loss}")
            print(f"Valid loss: {valid_loss}")
              
        print("Training finished!")
    
    def validation(self):
        print("Validation processing...")
        self.model.eval()
              
        valid_losses = []
        with torch.no_grad():
            for i, batch in enumerate(tqdm(self.valid_loader)):
                src_inputs, trg_inputs, trg_outputs = batch[:, :, 0], batch[:, :, 1], batch[:, :, 2]  # (B, T, L)
              
                dialogue_losses = []
                # Only used for context GRU.
                context = torch.zeros(src_inputs.shape[0], self.config['hidden_size']).to(self.config['device'])
                # Only used for ReCoSa.
                hists = torch.zeros(self.config['max_turn'], src_inputs.shape[0], self.config['d_model']).to(self.config['device'])
                for t in range(self.config['max_turn']):
                    if t < self.config['max_turn']-1:
                        src_input, trg_input, trg_output = \
                            src_inputs[:, t].to(self.config['device']), \
                            trg_inputs[:, t].to(self.config['device']), \
                            trg_outputs[: ,t].to(self.config['device'])  # (B, L)
                    
                        if self.config['model_type'] == 'gru':                              
                            output, context = self.model(src_input, trg_input, context)  # (B, L, vocab_size), (B, d_h)
                            
                        elif self.config['model_type'] == 'recosa':
                            output, hists = self.model(src_input, trg_input, hists, num_turn=t)  # (B, L, vocab_size), (T, B, d_model)
              
                        loss = self.criterion(
                            output.view(-1, self.config['vocab_size']),
                            trg_output.contiguous().view(output.shape[0] * output.shape[1])
                        )
              
                        dialogue_losses.append(loss.item())
                
                        del src_input, trg_input, trg_output
                        torch.cuda.empty_cache()
                    
                valid_losses += dialogue_losses
              
        mean_valid_loss = np.mean(valid_losses)
              
        return mean_valid_loss
        
              
    def test(self):
        print("Let's start!")
        print(f"If you want to quit the converstation, please type \"{self.config['end_command']}\".")
        self.model.eval()
        
        with torch.no_grad():
            utter = None
            
            # Only used for context GRU.
            context = torch.zeros(src_inputs.shape[0], self.config['hidden_size']).to(self.config['device'])
            # Only used for ReCoSa.
            hists = torch.zeros(self.config['max_turn'], src_inputs.shape[0], self.config['d_model']).to(self.config['device'])
            
            for t in range(self.config['max_turn']):
                if t % 2 == 0:
                    utter = input("You: ")
                    
                if utter == self.config['end_command']:
                    print("Bot: Good bye.")
                    break

                tokens = self.tokenizer.EncodeAsIds(utter)
                if len(tokens) < self.config['max_len']:
                    src_input = tokens + [self.config['eos_id']]
                    src_input += [self.config['pad_id']] * (self.config['max_len'] - len(src_input))
                else:
                    src_input = src_input[:self.config['max_len']]
                    src_input[-1] = self.config['eos_id']

                src_input = torch.LongTensor(src_input).unsqueeze(0).to(self.config['device'])  # (B, L)
                
                if self.config['model_type'] == 'gru':
                    src_emb = self.model.embed(src_input)  # (B, L, d_model)
                    e_mask = self.model.make_encoder_mask(src_input)  # (B, 1, L)
                    e_output = self.model.encoder(src_emb, e_mask)  # (B, L, d_model)
                    next_context = self.context_update(context, e_output)  # (B, d_model)
                    e_output = self.combine_context(e_output, context)  # (B, L, d_model)
                    context = next_context.clone()
                elif self.config['model_type'] == 'recosa':
                    src_emb, hists = self.model.src_embed(src_input, hists, num_turn)  # (B, L, d_model), (T, B, d_model)
                    e_mask = self.model.make_encoder_mask(t, src_input)  # (B, 1, L)
                    e_output = self.model.encoder(src_emb, e_mask)  # (B, L, 2*d_model)

                if t % 2 == 0:
                    output_ids = self.nucleus_sampling(e_output, e_mask)  # (L) list
                    utter = self.tokenizer.DecodeIds(output_ids)

                    print(f"Bot: {utter}")

                if t == self.config['max_turn']-1:
                    print("This is the last turn.")

    def nucleus_sampling(self, e_output, e_mask):
        trg_input = [self.config['bos_id']]
        trg_input += [self.config['pad_id']] * (self.config['max_len']-len(trg_input))
        trg_input = torch.LongTensor(trg_input).unsqueeze(0).to(self.config['device'])  # (B, L)
        
        output_ids = []
        
        for pos in range(self.config['max_len']):
            if self.config['model_type'] == 'gru':
                trg_emb = self.model.embed(trg_input)  # (B, L, d_model)
            elif self.config['model_type'] == 'recosa':
                trg_emb = self.model.trg_embed(trg_input)  # (B, L, 2*d_model)
            d_mask = self.model.make_decoder_mask(trg_input)  # (B, L, L)
            d_output = self.model.decoder(trg_emb, e_output, e_mask, d_mask)  # (B, L, d_model) or (B, L, 2*d_model)
            
            output = F.softmax(self.model.output_linear(d_output), dim=-1)  # (B, L, vocab_size)
            output = output[:,pos]  # (B, vocab_size)
            
            sorted_probs, sorted_idxs = torch.sort(output, descending=True)
            cumsum_probs = torch.cumsum(sorted_probs, dim=-1)  # (B, vocab_size)
            idx_remove = cumsum_probs > self.config['nucleus_p']
            sorted_probs[idx_remove] = 1e-8
            sorted_probs /= torch.sum(sorted_probs, dim=-1, keepdim=True)  # (B, vocab_size)
            
            # Random sampling
            seed = int(time.time())
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            probs = torch.zeros(output.shape).to(self.config['device']).scatter_(-1, sorted_idxs, sorted_probs)  # (B, vocab_size)
            idxs = torch.multinomial(probs, 1).squeeze(-1)  # (B)
            
            if pos < self.config['max_len']-1:
                trg_input[:, pos+1] = idxs
            
            output_ids.append(idxs.squeeze(0).item())    
            if idxs.squeeze(0).item() == self.config['eos_id']:
                break
            
            del output, sorted_probs, sorted_idxs, cumsum_probs, idx_remove, probs, idxs
            torch.cuda.empty_cache()
            
        if output_ids[-1]== self.config['eos_id']:
            output_ids = output_ids[:-1]
            
        return output_ids
        

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', required=True, help="Train or test?")
    parser.add_argument('--model_type', required=True, help="Context history method.")
    parser.add_argument('--ckpt_name', required=False, help="Best checkpoint file.")
              
    args = parser.parse_args()
    
    assert args.mode == 'train' or args.mode=='test', print("Please specify a correct mode name, 'train' or 'test'.")
    assert args.model_type == 'gru' or args.model_type=='recosa', print("Please specify a correct model type, 'gru' or 'recosa'.")
              
    if args.mode == 'train':
        if args.ckpt_name is not None:
            manager = Manager(args.mode, args.model_type, ckpt_name=args.ckpt_name)
        else:
            manager = Manager(args.mode, args.model_type)
              
        manager.train()
        
    elif args.mode == 'test':
        assert args.ckpt_name is not None, "Please specify the trained model checkpoint."
        
        manager = Manager(args.mode, args.model_type, ckpt_name=args.ckpt_name)
        
        manager.test()
