import torch.optim as optim
import torch
import torch.nn as nn
from pathlib import Path
from miditok import REMI
from miditok.pytorch_data import DatasetMIDI, DataCollator
from torch.utils.data import DataLoader
from utils import generate_causal_mask
from utils import load_pretrain_data, split_pretrain_data
from random import shuffle
from miditok.data_augmentation import augment_dataset
from midi_decoder import MidiDecoderOnlyModel
from tqdm import tqdm
import logging
import os


def main():
    
    logging.basicConfig(
        filename='pretrain_log.log',
        level=logging.INFO,
        format='%(asctime)s — %(levelname)s — %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    os.makedirs("pretrain_checkpoints", exist_ok=True)

    midi_tokenizer = REMI()

    # download and split pretrain data only if the folder does not exist
    if not os.path.exists("midis"):
        pretrain_file_id = "1BDEPaEWFEB2ADquS1VYp5iLZYVngw799"
        url = f"https://drive.google.com/uc?id={pretrain_file_id}"
        
        load_pretrain_data(url, "midis.zip", "midis")
        split_pretrain_data("midis", midi_tokenizer, 1024)
        
    midi_paths = list(Path("pretrain_data/dataset_train").resolve().glob("**/*.mid"))

    dataset = DatasetMIDI(
        files_paths=midi_paths,
        tokenizer=midi_tokenizer,
        max_seq_len=1024,
        bos_token_id=midi_tokenizer.pad_token_id,
        eos_token_id=midi_tokenizer["BOS_None"],
    )
    
    collator = DataCollator(midi_tokenizer.pad_token_id)
    data_loader = DataLoader(dataset=dataset, collate_fn=collator, batch_size=16)
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = MidiDecoderOnlyModel(vocab_size=len(midi_tokenizer.vocab))
    
    if torch.cuda.device_count() > 1:
        print(f"Using {torch.cuda.device_count()} GPUs")
        model = nn.DataParallel(model)
    
    model = model.to(device)

    criterion = nn.CrossEntropyLoss(ignore_index=midi_tokenizer.pad_token_id)
    optimizer = optim.AdamW(model.parameters(), lr=1e-4)
    
    # ==== Resume checkpoint ====
    checkpoint_path = "pretrain_checkpoints/decoder_epoch_4.pt"  # change if needed
    start_epoch = 0

    if os.path.exists(checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        model.module.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch'] + 1
        print(f"Loaded checkpoint from epoch {checkpoint['epoch']}")
        logging.info(f"Resumed training from checkpoint: epoch {checkpoint['epoch']}")
        
        
    num_epochs = 20
    save_every = 2

    model.train()
    step = 0
    log_interval = 500
    for epoch in range(start_epoch, num_epochs):
        total_loss = 0
        for _, batch in enumerate(tqdm(data_loader)):
            step += 1

            input_ids = batch['input_ids'].to(device)            # (batch_size, seq_len)
            attention_mask = batch['attention_mask'].to(device)  # (batch_size, seq_len)

            decoder_input = input_ids[:, :-1]        # (batch_size, seq_len - 1)
            target = input_ids[:, 1:]                # (batch_size, seq_len - 1)
            attn_mask = attention_mask[:, :-1]       # (batch_size, seq_len - 1)

            tgt_key_padding_mask = (attn_mask == 0)

            output = model(
                decoder_input,
                tgt_key_padding_mask=tgt_key_padding_mask,
            )  # (batch_size, seq_len - 1, vocab)

            loss = criterion(output.reshape(-1, output.size(-1)), target.reshape(-1))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            
            total_loss += loss.item()

            if step % log_interval == 0:
                avg_train_loss = total_loss / step
                log_msg = f"step {step} - Loss: {avg_train_loss:.4f}"
                logging.info(log_msg)
            

        log_msg = f"Epoch {epoch+1} — Loss: {total_loss / len(data_loader):.4f}"
        print(log_msg) 
        logging.info(log_msg)

        if epoch % save_every == 0 and epoch != 0:
            checkpoint_path = f"pretrain_checkpoints/decoder_epoch_{epoch}.pt"
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.module.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss.item(),
            }, checkpoint_path)
            print(f"Saved checkpoint: {checkpoint_path}")


    
if __name__ == "__main__":
    main()
