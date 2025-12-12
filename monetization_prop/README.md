## downloading python=3.11 with conda
sometimes evenif you have conda installed you have to add channels too, to start installing:

```bash
    conda config --add channels defaults
    conda config --add channels conda-forge

    conda install python=3.11
```

## Install cog cli
```bash
sudo curl -o /usr/local/bin/cog -L https://github.com/replicate/cog/releases/latest/download/cog_`uname -s`_`uname -m`
sudo chmod +x /usr/local/bin/cog

cog init
```
## Running out of spaces check cached pip libs

```bash
# Clear all pip cache
pip cache purge

# Or manually delete the cache directory
rm -rf ~/.cache/pip
```