import pandas as pd
import torch
import torch.nn as nn
from torch.nn import functional as F
import numpy as np
from sklearn.model_selection import train_test_split

block_size = 17
batch_size = 8
max_iters = 5000
eval_interval = 500
learning_rate = 3e-6
device = 'cuda' if torch.cuda.is_available() else 'cpu'
eval_iters = 200
n_embd = 384
n_head = 6
n_layer = 6
dropout = 0.2

dataset = pd.read_csv('../intermediate_data/ssp1.csv')

y = dataset.pop('humanitarian_needs').values.astype(float)
y = np.round(y, -6).astype(int) # Nearest million
dataset.pop('humanitarian')
y_min = np.min(y)
y_max = np.max(y)

X = dataset.values.astype(float)
X_min = np.min(X)
X_max = np.max(X)

def scale_X(x):
    normal_x = (x - X_min) / (X_max - X_min)
    scaled_to_y = normal_x * (y_max - y_min) + y_min
    return scaled_to_y

def unscale_X(x):
    normal_x = (x - y_min) / (y_max - y_min)
    scaled_to_x = normal_x * (X_max - X_min) + X_min
    return scaled_to_x

X = scale_X(X)
X = np.round(X, -6).astype(int)

chars = sorted(np.unique(
    np.concatenate([
        np.unique(X),
        np.unique(y)
    ])
))
vocab_size = len(chars)
print("Vocab size: ", vocab_size)

stoi = { ch:i for i,ch in enumerate(chars) }
itos = { i:ch for i,ch in enumerate(chars) }
encode = lambda s: [stoi[c] for c in s] # encoder: take a string, output a list of integers
decode = lambda l: ','.join([str(itos[i]) for i in l]) # decoder: take a list of integers, output a string

# Train and test splits
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.1)
X_train_data = torch.tensor(np.apply_along_axis(encode, 1, X_train), dtype=torch.long)
X_test_data = torch.tensor(np.apply_along_axis(encode, 1, X_test), dtype=torch.long)
y_train_data = torch.tensor(encode(y_train), dtype=torch.long)
y_test_data = torch.tensor(encode(y_test), dtype=torch.long)


def get_batch(split):
    # generate a small batch of data of inputs x and targets y
    data = X_train_data if split == 'train' else X_test_data
    targets = y_train_data if split == 'train' else y_test_data
    ix = torch.randint(len(data), (batch_size,))
    x = torch.stack([data[i] for i in ix])
    y = torch.stack([targets[i] for i in ix])
    y.unsqueeze_(-1)
    y = y.expand(batch_size, block_size)
    x, y = x.to(device), y.to(device)
    return x, y

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

class Head(nn.Module):
    """ one head of self-attention """

    def __init__(self, head_size):
        super().__init__()
        self.key = nn.Linear(n_embd, head_size, bias=False)
        self.query = nn.Linear(n_embd, head_size, bias=False)
        self.value = nn.Linear(n_embd, head_size, bias=False)
        self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))

        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        # input of size (batch, time-step, channels)
        # output of size (batch, time-step, head size)
        B,T,C = x.shape
        k = self.key(x)   # (B,T,hs)
        q = self.query(x) # (B,T,hs)
        # compute attention scores ("affinities")
        wei = q @ k.transpose(-2,-1) * k.shape[-1]**-0.5 # (B, T, hs) @ (B, hs, T) -> (B, T, T)
        # wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf')) # (B, T, T)
        wei = F.softmax(wei, dim=-1) # (B, T, T)
        wei = self.dropout(wei)
        # perform the weighted aggregation of the values
        v = self.value(x) # (B,T,hs)
        out = wei @ v # (B, T, T) @ (B, T, hs) -> (B, T, hs)
        return out

class MultiHeadAttention(nn.Module):
    """ multiple heads of self-attention in parallel """

    def __init__(self, num_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size) for _ in range(num_heads)])
        self.proj = nn.Linear(head_size * num_heads, n_embd)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([h(x) for h in self.heads], dim=-1)
        out = self.dropout(self.proj(out))
        return out

class FeedFoward(nn.Module):
    """ a simple linear layer followed by a non-linearity """

    def __init__(self, n_embd):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_embd, 4 * n_embd),
            nn.ReLU(),
            nn.Linear(4 * n_embd, n_embd),
            nn.Dropout(dropout),
        )

    def forward(self, x):
        return self.net(x)

class Block(nn.Module):
    """ Transformer block: communication followed by computation """

    def __init__(self, n_embd, n_head):
        # n_embd: embedding dimension, n_head: the number of heads we'd like
        super().__init__()
        head_size = n_embd // n_head
        self.sa = MultiHeadAttention(n_head, head_size)
        self.ffwd = FeedFoward(n_embd)
        self.ln1 = nn.LayerNorm(n_embd)
        self.ln2 = nn.LayerNorm(n_embd)

    def forward(self, x):
        x = x + self.sa(self.ln1(x))
        x = x + self.ffwd(self.ln2(x))
        return x

class GPTTabularModel(nn.Module):

    def __init__(self):
        super().__init__()
        # each token directly reads off the logits for the next token from a lookup table
        self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
        self.position_embedding_table = nn.Embedding(block_size, n_embd)
        self.blocks = nn.Sequential(*[Block(n_embd, n_head=n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_embd) # final layer norm
        self.lm_head = nn.Linear(n_embd, vocab_size)

        # better init, not covered in the original GPT video, but important, will cover in followup video
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        B, T = idx.shape

        # idx and targets are both (B,T) tensor of integers
        tok_emb = self.token_embedding_table(idx) # (B,T,C)
        pos_emb = self.position_embedding_table(torch.arange(T, device=device)) # (T,C)
        x = tok_emb + pos_emb # (B,T,C)
        x = self.blocks(x) # (B,T,C)
        x = self.ln_f(x) # (B,T,C)
        logits = self.lm_head(x) # (B,T,vocab_size)

        if targets is None:
            loss = None
        else:
            B, T, C = logits.shape
            logits = logits.view(B*T, C)
            targets = targets.reshape(B*T)
            loss = F.cross_entropy(logits, targets)

        return logits, loss

    def generate(self, idx, max_new_tokens):
        # idx is (B, T) array of indices in the current context
        for _ in range(max_new_tokens):
            # crop idx to the last block_size tokens
            idx_cond = idx[:, -block_size:]
            # get the predictions
            logits, loss = self(idx_cond)
            # focus only on the last time step
            logits = logits[:, -1, :] # becomes (B, C)
            # apply softmax to get probabilities
            probs = F.softmax(logits, dim=-1) # (B, C)
            # sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1) # (B, 1)
            # append sampled index to the running sequence
            idx = torch.cat((idx, idx_next), dim=1) # (B, T+1)
        return idx

model = GPTTabularModel()
m = model.to(device)
# print the number of parameters in the model
print(sum(p.numel() for p in m.parameters())/1e6, 'M parameters')

# create a PyTorch optimizer
optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)

for iter in range(max_iters):

    # every once in a while evaluate the loss on train and val sets
    if iter % eval_interval == 0 or iter == max_iters - 1:
        losses = estimate_loss()
        print(f"step {iter}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

    # sample a batch of data
    xb, yb = get_batch('train')

    # evaluate the loss
    logits, loss = model(xb, yb)
    optimizer.zero_grad(set_to_none=True)
    loss.backward()
    optimizer.step()

# generate from the model
header = [
    "conflict",
    "temp",
    "nb_conflict",
    "YMHEP",
    "lpop",
    "lGDPcap",
    "ltsc0",
    "nc",
    "ncc1",
    "ncc2",
    "ltsnc",
    "ncts0",
    "lpop.1",
    "lGDPcap_c1",
    "lGDPcap_c2",
    "lGDPcap_ltsc0",
    "ltimeindep",
    "humanitarian_needs_actual",
    "humanitarian_needs_prediction"
]

with open('transformer_output.csv', 'w') as csv_file:
    csv_file.write(','.join(header) + '\n')
    for i in range(0, len(X_test_data)):
        actual = str(y_test[i].tolist())
        context = X_test_data[i].expand(1, block_size)
        row = ','.join([str(unscale_X(cell)) for cell in X_test[i].tolist()])
        prediction = decode([m.generate(context, max_new_tokens=1)[0].tolist()[-1]])
        row = row + ',' + actual + ',' + prediction + '\n'
        csv_file.write(row)
