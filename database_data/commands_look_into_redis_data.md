### commands used to look into redis cache data

To Read Redis data cache, its in byte format so can use tools to convert it to json with 
- pip install rdbtools python-lzf
```bash
rdb --command json dump.rdb > redis_content.json
cat redis_content.json
```