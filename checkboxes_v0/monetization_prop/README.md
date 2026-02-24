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
ocker builder prune -fd
docker system prune -f
docker system prune -a --volumes -f


### Running and installation of stripe Cli 
##### Download the latest release (replace version with latest if needed)
curl -L https://github.com/stripe/stripe-cli/releases/download/v1.21.0/stripe_1.21.0_linux_x86_64.tar.gz -o stripe.tar.gz

##### Extract the tarball
tar -xvzf stripe.tar.gz

##### Move the binary to /usr/local/bin so it's in your PATH
sudo mv stripe /usr/local/bin/

##### Verify installation
stripe version
##### login to verify access
stripe login

##### forward stripe cli to localhost
stripe listen --forward-to localhost:5000/webhook


## ngrok installtion
```bash
    curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
    | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
    && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
    | sudo tee /etc/apt/sources.list.d/ngrok.list \
    && sudo apt update \
    && sudo apt install ngrok
```
## Running out of spaces check cached pip libs

```bash
# Clear all pip cache
pip cache purge

# Or manually delete the cache directory
rm -rf ~/.cache/pip
```