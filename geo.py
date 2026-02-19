import httpx, json
from urllib.parse import urlparse
from typing import Dict, Any, Optional


async def get_geo_from_providers(ip:str, redis):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"https://ipwho.is/{ip}?security=1")
        if r.status_code == 200 and (data := r.json()).get("success"):
            print(f"[GEO] ✅ ipwho.is succesfully resolved {ip} -> {data.get('city')}, {data.get('country')}")
            sec ,conn = data.get("security", {}), data.get("connection", {})
            org_lower = conn.get("org", "").lower()
            usage =("Data Center" if sec.get("hosting") else "Education" if any(x in org_lower for x in ["uni", "college", "school"])
                     else "Business" if any(x in org_lower for x in ["corp", "inc", "ltd"]) else "Cellular" if data.get("type") =="Mobile" else "Residentail")
            is_relay_val = sec.get("relay", False) or "icloud" in conn.get("isp", "").lower() or "apple" in org_lower
            return{ "ip": ip, "city": data.get("city"), "postal": data.get("postal"), "country": data.get("country"), "region": data.get("region"),
                    "is_vpn": sec.get("vpn", False) or sec.get("proxy", False), "isp": conn.get("isp"), "is_hosting": sec.get("hosting", False),
                    "org": conn.get("org"), "asn": conn.get("asn"), "usage_type": usage, "is_relay": is_relay_val, "provider": "ipwho.is" }
    except Exception: pass
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://ip-api.com/json/{ip}?fields=66842239")
        if r.status_code == 200 and (data := r.json()).get("status") == "success":
            usage = "Data Center" if data.get("hosting") else "Cellular" if data.get("mobile") else "Residential"
            org_lower, isp_lower = data.get("isp", "").lower(), data.get("org", "").lower()
            is_relay_val = data.get("proxy", False) or any(x in isp_lower or x in org_lower for x in ["icloud", "apple relay", "apple inc"])
            print(f"[GEO] ✅ ip-api.com succesfully resolved {ip}")
            return { "ip": ip, "city": data.get("city"), "isp": data.get("isp"), "usage_type": "Privacy Relay" if is_relay_val else usage,
                     "is_vpn": data.get("proxy", False), "is_hosting": data.get("hosting", False), "is_relay": is_relay_val, "provider": "ip-api.com" }
    except Exception as e:  print(f"[GEO] ❌ ip-api.com failed for  {ip}: {e}")
    return {"ip": ip, "usage_type": "Unknown", "city": None, "country": None, "zip": None}


async def get_geo(ip: str, redis):
    """Return geo info from ip using cache + fallback providers"""
    if (cached := await redis.get(f"geo:{ip}")):
        print(f"[GEO] 💾 Cache hit for {ip}")
        return json.loads(cached)
    print(f"[GEO]  🔍 Cache miss for {ip}, fetching from providers...")
    data = await get_geo_from_providers(ip,redis)
    try:
        await redis.set(f"geo:{ip}", json.dumps(data),ex=604800) #save get_geo api calls to providers #, ex=GEO_TTL_REDIS) ,604800-> 7 days
        print(f"[GEO] 💾 Cached geo data for {ip}")
    except Exception as e: print(f"[GEO] ⚠️  Failed to cache geo data for {ip}: {e}")
    return data

# # OPTIONS: "cache" = use cached data, "fresh" = force new lookup, "rollback" = restore from backup
# GEO_MODE = "fresh"

# async def get_geo(ip: str, redis):
#     """Return geo info from ip using cache + fallback providers"""
    
#     if GEO_MODE == "rollback":
#         if (backup := await redis.get(f"geo:backup:{ip}")):
#             print(f"[GEO] ⏪ Rolling back to backup for {ip}")
#             await redis.set(f"geo:{ip}", backup)#, ex=86400)
#             return json.loads(backup)
#         print(f"[GEO] ⚠️ No backup found for {ip}, falling through to cache...")

#     if GEO_MODE == "fresh":
#         old = await redis.get(f"geo:{ip}")
#         if old: 
#             await redis.set(f"geo:backup:{ip}", old)  # save backup before overwriting
#             print(f"[GEO] 💾 Backed up old data for {ip}")
#         print(f"[GEO] 🔄 Forcing fresh lookup for {ip}")
#         data = await get_geo_from_providers(ip, redis)
#         try: await redis.set(f"geo:{ip}", json.dumps(data))#, ex=86400)
#         except Exception as e: print(f"[GEO] ⚠️ Failed to cache geo data for {ip}: {e}")
#         return data

#     # default: GEO_MODE == "cache"
#     if (cached := await redis.get(f"geo:{ip}")):
#         print(f"[GEO] 💾 Cache hit for {ip}")
#         return json.loads(cached)
#     print(f"[GEO] 🔍 Cache miss for {ip}, fetching from providers...")
#     data = await get_geo_from_providers(ip, redis)
#     try: await redis.set(f"geo:{ip}", json.dumps(data))#, ex=86400)
#     except Exception as e: print(f"[GEO] ⚠️ Failed to cache geo data for {ip}: {e}")
#     return data
