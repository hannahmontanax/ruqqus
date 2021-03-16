import requests
from os import environ, remove
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from PIL import Image as PILimage
from flask import g
from io import BytesIO
import time

from .get import *
from ruqqus.__main__ import app, db_session


def thumbnail_thread(pid, debug=False):

    db = db_session()

    post = get_post(pid, graceful=True, session=db)
    if not post:
        # account for possible follower lag
        time.sleep(60)
        post = get_post(pid, session=db)

    # step 1: see if post is image

    #print("thumbnail thread")

    domain_obj = post.domain_obj

    #mimic chrome browser agent
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.72 Safari/537.36"}


    if debug:
        print(f"domain_obj {domain_obj}")
        if domain_obj:
            print(f"show thumb {domain_obj.show_thumbnail}")

    if domain_obj and domain_obj.show_thumbnail:

        if debug:
            print("trying direct url as image")

        if post.embed_url and post.embed_url.startswith("https://"):
            fetch_url=post.embed_url
        else:
            fetch_url=post.url

        try:
            x = requests.get(fetch_url, headers=headers)
        except:
            if debug:
                print("error connecting")
            return

        if x.status_code>=400:
            return

        if x.headers.get("Content-Type", "/").split("/")[0] == "image":
            # image post, using submitted url

            name = f"posts/{post.base36id}/thumb.png"
            tempname = name.replace("/", "_")

            with open(tempname, "wb") as file:
                for chunk in x.iter_content(1024):
                    file.write(chunk)

            aws.upload_from_file(name, tempname, resize=(375, 227))
            post.has_thumb = True

            post.is_image = True
            db.add(post)

            db.commit()
            return

    if debug:
        print("not direct image")
    try:
        x = requests.get(post.url, headers=headers)

    except:
        if debug:
            print("error connecting")
        return

    if x.status_code != 200 or not x.headers["Content-Type"].startswith(
            ("text/html", "image/")):
        if debug:
            print(f'not html post, status {x.status_code}')
        return

    if x.headers["Content-Type"].startswith("image/"):

        if debug:
            print("submitted url is image, use that")
        pass
        # submitted url is image

    elif x.headers["Content-Type"].startswith("text/html"):


        if debug:
            print("parsing html doc")

        soup = BeautifulSoup(x.content, 'html.parser')

        #get meta title and description
        try:
            meta_title=soup.find('title')
            if meta_title:
                post.submission_aux.meta_title=str(meta_title.string)

            meta_desc = soup.find('meta', attrs={"name":"description"})
            if meta_desc:
                post.submission_aux.meta_description=meta_desc['content']

            if meta_title or meta_desc:
                db.add(post.submission_aux)
        except:
            pass

        metas = ["ruqqus:thumbnail",
                 "twitter:image",
                 "og:image",
                 "thumbnail"
                 ]

        for meta in metas:

            if debug:
                print(f"Looking for meta tag: {meta}")

            img = soup.find('meta', attrs={"name": meta, "content": True})
            if not img:
                img = soup.find(
                    'meta',
                    attrs={
                        'property': meta,
                        'content': True})
            if not img:
                continue
            try:
                if debug:
                    print(f"image load attempt from meta tag {meta}")
                x = requests.get(img['content'], headers=headers)
            except BaseException:
                if debug:
                    print("unable to connect")
                continue
            break

        if debug:
            print(img)
            print(x)

        if not img or not x or x.status_code != 200:

            if debug:
                print("no meta tags, looking for img")

            imgs = soup.find_all('img', src=True)
            if debug:
                print(f"found {len(imgs)} img elements")
            if imgs:
                #print("using <img> elements")
                pass
            else:
                #print('no image in doc')
                return

            # Loop through all images in document until we find one that works
            # (and isn't svg)
            for img in imgs:

                src = img["src"]

                #print("raw src: "+src)

                # convert src into full url
                if src.startswith("https://"):
                    pass
                elif src.startswith("http://"):
                    src = f"https://{src.split('http://')[1]}"
                elif src.startswith('//'):
                    src = f"https:{src}"
                elif src.startswith('/'):
                    parsed_url = urlparse(post.url)
                    src = f"https://{parsed_url.netloc}{src}"
                else:
                    src = f"{post.url}{'/' if not post.url.endswith('/') else ''}{src}"

                #print("full src: "+src)

                # load asset

                if debug:
                    print(f"attempting asset load {src}")
                x = requests.get(src, headers=headers)

                if x.status_code != 200:
                    if debug:
                        print(f"status code {x.status_code}, try next")
                    #print('not 200, next')
                    continue

                type = x.headers.get("Content-Type", "")

                if not type.startswith("image/"):
                    if debug:
                        print(f"bad type {type}, try next")
                    #print("not an image, next")
                    continue

                if type.startswith("image/svg"):
                    if debug:
                        print("svg, try next")
                    #print("svg image, next")
                    continue

                i = PILimage.open(BytesIO(x.content))
                if i.width < 30 or i.height < 30:
                    if debug:
                        print("image too small, next")
                    continue

                break
        else:
            if debug:
                print("meta tag found, no need to look for img tags")

    name = f"posts/{post.base36id}/thumb.png"
    tempname = name.replace("/", "_")

    with open(tempname, "wb") as file:
        for chunk in x.iter_content(1024):
            file.write(chunk)

    aws.upload_from_file(name, tempname, resize=(375, 227))
    post.has_thumb = True
    db.add(post)

    db.commit()

    # db.close()

    try:
        remove(tempname)
    except FileNotFoundError:
        pass
