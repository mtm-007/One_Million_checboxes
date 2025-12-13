## downloading python=3.11 with conda, or just use the latest lib versions for all with python=3.12
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


# running cog docker build and model inference
```bash
run:
- time sudo cog predict -i prompt="wakkanda forever"
```

# Pushing to cog replicate, run with sudo to avoid authentication issues
sudo cog login
authenticate replicate api
sudo chown -R $USER:$USER .
sudo cog push r8.im/<replicate_username>/<model_name>

### check cog docker size
docker images | grep cog-

### clean docker build images
docker rmi cog-monetizationprop cog-monetizationprop-base
docker builder prune -f
docker system prune -f
docker system prune -a --volumes -f

## Running out of spaces check cached pip libs

```bash
# Clear all pip cache
pip cache purge

# Or manually delete the cache directory
rm -rf ~/.cache/pip
```