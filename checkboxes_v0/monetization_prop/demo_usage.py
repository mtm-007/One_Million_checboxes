import replicate
# replicate = replicate.Client(api_token="")
# output = replicate.run(
#     "model_repo",
#     input = {"prompt": "an own wearing a hat"}
# )
# print(output)

output = replicate.run(
    "mtm-007/custm_diffusion:4536d1407dddcdc365ecaee695283e2b1d3307bb214550c8e35d245abada3994",
    input={
        "prompt": "An owl wearing a hat"
    }
)

# To access the file URL:
print(output)
#=> "http://example.com"


# # To write the file to disk:
# with open("my-image.png", "wb") as file:
#     file.write(output.read())