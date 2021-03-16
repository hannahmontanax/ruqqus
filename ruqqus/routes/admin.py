from urllib.parse import urlparse
import time
import calendar
from sqlalchemy import func
from sqlalchemy.orm import lazyload
import threading
import subprocess
import imagehash
from os import remove
from PIL import Image as IMAGE

from ruqqus.helpers.wrappers import *
from ruqqus.helpers.alerts import *
from ruqqus.helpers.base36 import *
from ruqqus.helpers.sanitize import *
from ruqqus.helpers.get import *
from ruqqus.classes import *
from ruqqus.classes.domains import reasons as REASONS
from ruqqus.routes.admin_api import create_plot, user_stat_data
from ruqqus.classes.categories import CATEGORIES
from flask import *

import ruqqus.helpers.aws as aws
from ruqqus.__main__ import app


@app.route("/admin/flagged/posts", methods=["GET"])
@admin_level_required(3)
def flagged_posts(v):

    page = max(1, int(request.args.get("page", 1)))

    posts = g.db.query(Submission).filter_by(
        is_approved=0,
        purged_utc=0,
        is_banned=False
    ).join(Submission.flags
           ).options(contains_eager(Submission.flags)
                     ).order_by(Submission.id.desc()).offset(25 * (page - 1)).limit(26)

    listing = [p.id for p in posts]
    next_exists = (len(listing) == 26)
    listing = listing[0:25]

    listing = get_posts(listing, v=v)

    return render_template("admin/flagged_posts.html",
                           next_exists=next_exists, listing=listing, page=page, v=v)


@app.route("/admin/image_posts", methods=["GET"])
@admin_level_required(3)
@api("read")
def image_posts_listing(v):

    page = int(request.args.get('page', 1))

    posts = g.db.query(Submission).filter_by(domain_ref=1).order_by(Submission.id.desc()
                                                                    ).offset(25 * (page - 1)
                                                                             ).limit(26)

    posts = [x.id for x in posts]
    next_exists = (len(posts) == 26)
    posts = posts[0:25]

    posts = get_posts(posts, v=v)

    return {'html': lambda: render_template("admin/image_posts.html",
                                            v=v,
                                            listing=posts,
                                            next_exists=next_exists,
                                            page=page,
                                            sort_method="new"
                                            ),
            'api': lambda: [x.json for x in posts]
            }


@app.route("/admin/flagged/comments", methods=["GET"])
@admin_level_required(3)
def flagged_comments(v):

    page = max(1, int(request.args.get("page", 1)))

    posts = g.db.query(Comment
                       ).filter_by(
        is_approved=0,
        purged_utc=0,
        is_banned=False
    ).join(Comment.flags).options(contains_eager(Comment.flags)
                                  ).order_by(Comment.id.desc()).offset(25 * (page - 1)).limit(26).all()

    listing = [p.id for p in posts]
    next_exists = (len(listing) == 26)
    listing = listing[0:25]

    listing = get_comments(listing, v=v)

    return render_template("admin/flagged_comments.html",
                           next_exists=next_exists,
                           listing=listing,
                           page=page,
                           v=v,
                           standalone=True)


# @app.route("/admin/<path>", methods=["GET"])
# @admin_level_required(3):
# def admin_path(v):
# try:
# return render_template(safe_join("admin", path+".html"), v=v)
# except jinja2.exceptions.TemplateNotFound:
# abort(404)

@app.route("/admin", methods=["GET"])
@admin_level_required(3)
def admin_home(v):
    return render_template("admin/admin_home.html", v=v)


@app.route("/admin/badge_grant", methods=["GET"])
@admin_level_required(4)
def badge_grant_get(v):

    badge_types = g.db.query(BadgeDef).filter_by(
        kind=3).order_by(BadgeDef.rank).all()

    errors = {"already_owned": "That user already has that badge.",
              "no_user": "That user doesn't exist."
              }

    return render_template("admin/badge_grant.html",
                           v=v,
                           badge_types=badge_types,
                           error=errors.get(
                               request.args.get("error"),
                               None) if request.args.get('error') else None,
                           msg="Badge successfully assigned" if request.args.get(
                               "msg") else None
                           )


@app.route("/badge_grant", methods=["POST"])
@admin_level_required(4)
@validate_formkey
def badge_grant_post(v):

    user = get_user(request.form.get("username"), graceful=True)
    if not user:
        return redirect("/badge_grant?error=no_user")

    badge_id = int(request.form.get("badge_id"))

    if user.has_badge(badge_id):
        return redirect("/badge_grant?error=already_owned")

    badge = g.db.query(BadgeDef).filter_by(id=badge_id).first()
    if badge.kind != 3:
        abort(403)

    new_badge = Badge(badge_id=badge_id,
                      user_id=user.id,
                      created_utc=int(time.time())
                      )

    desc = request.form.get("description")
    if desc:
        new_badge.description = desc

    url = request.form.get("url")
    if url:
        new_badge.url = url

    g.db.add(new_badge)

    g.db.commit()

    text = f"""
@{v.username} has given you the following profile badge:
\n\n![]({new_badge.path})
\n\n{new_badge.name}
"""

    send_notification(user, text)

    return redirect(user.permalink)


@app.route("/admin/users", methods=["GET"])
@admin_level_required(2)
def users_list(v):

    page = int(request.args.get("page", 1))

    users = g.db.query(User).filter_by(is_banned=0
                                       ).order_by(User.created_utc.desc()
                                                  ).offset(25 * (page - 1)).limit(26)

    users = [x for x in users]

    next_exists = (len(users) == 26)
    users = users[0:25]

    return render_template("admin/new_users.html",
                           v=v,
                           users=users,
                           next_exists=next_exists,
                           page=page,
                           )

@app.route("/admin/data", methods=["GET"])
@admin_level_required(2)
def admin_data(v):

    data = user_stat_data().get_json()

    return render_template("admin/new_users.html",
                           v=v,
                           next_exists=False,
                           page=1,
                           single_plot=data['single_plot'],
                           multi_plot=data['multi_plot']
                           )


@app.route("/admin/content_stats", methods=["GET"])
@admin_level_required(2)
def participation_stats(v):

    now = int(time.time())

    data = {"valid_users": g.db.query(User).filter_by(is_deleted=False).filter(or_(User.is_banned == 0, and_(User.is_banned > 0, User.unban_utc > 0))).count(),
            "private_users": g.db.query(User).filter_by(is_deleted=False, is_private=False).filter(User.is_banned > 0, or_(User.unban_utc > now, User.unban_utc == 0)).count(),
            "banned_users": g.db.query(User).filter(User.is_banned > 0, User.unban_utc == 0).count(),
            "deleted_users": g.db.query(User).filter_by(is_deleted=True).count(),
            "locked_negative_users": g.db.query(User).filter(User.negative_balance_cents>0).count(),
            "total_posts": g.db.query(Submission).count(),
            "posting_users": g.db.query(Submission.author_id).distinct().count(),
            "listed_posts": g.db.query(Submission).filter_by(is_banned=False).filter(Submission.deleted_utc > 0).count(),
            "removed_posts": g.db.query(Submission).filter_by(is_banned=True).count(),
            "deleted_posts": g.db.query(Submission).filter(Submission.deleted_utc > 0).count(),
            "total_comments": g.db.query(Comment).count(),
            "commenting_users": g.db.query(Comment.author_id).distinct().count(),
            "removed_comments": g.db.query(Comment).filter_by(is_banned=True).count(),
            "deleted_comments": g.db.query(Comment).filter(Comment.deleted_utc>0).count(),
            "total_guilds": g.db.query(Board).count(),
            "listed_guilds": g.db.query(Board).filter_by(is_banned=False, is_private=False).count(),
            "private_guilds": g.db.query(Board).filter_by(is_banned=False, is_private=True).count(),
            "banned_guilds": g.db.query(Board).filter_by(is_banned=True).count(),
            "post_votes": g.db.query(Vote).count(),
            "post_voting_users": g.db.query(Vote.user_id).distinct().count(),
            "comment_votes": g.db.query(CommentVote).count(),
            "comment_voting_users": g.db.query(CommentVote.user_id).distinct().count()
            }

    #data = {x: f"{data[x]:,}" for x in data}

    return render_template("admin/content_stats.html", v=v, title="Content Statistics", data=data)


@app.route("/admin/money", methods=["GET"])
@admin_level_required(2)
def money_stats(v):

    now = time.gmtime()
    midnight_year_start = time.struct_time((now.tm_year,
                                              1,
                                              1,
                                              0,
                                              0,
                                              0,
                                              now.tm_wday,
                                              now.tm_yday,
                                              0)
                                             )
    midnight_year_start = calendar.timegm(midnight_year_start)

    now=int(time.time())
    intake=sum([int(x[0] - (x[0] * 0.029) - 30 )  for x in g.db.query(PayPalTxn.usd_cents).filter(PayPalTxn.status==3, PayPalTxn.created_utc>midnight_year_start).all()])
    loss=sum([x[0] for x in g.db.query(PayPalTxn.usd_cents).filter(PayPalTxn.status<0, PayPalTxn.created_utc>midnight_year_start).all()])
    revenue=str(intake-loss)

    data={
        "cents_received_last_24h":g.db.query(func.sum(PayPalTxn.usd_cents)).filter(PayPalTxn.status==3, PayPalTxn.created_utc>now-60*60*24).scalar(),
        "cents_received_last_week":g.db.query(func.sum(PayPalTxn.usd_cents)).filter(PayPalTxn.status==3, PayPalTxn.created_utc>now-60*60*24*7).scalar(),
        "sales_count_last_24h":g.db.query(PayPalTxn).filter(PayPalTxn.status==3, PayPalTxn.created_utc>now-60*60*24).count(),
        "sales_count_last_week":g.db.query(PayPalTxn).filter(PayPalTxn.status==3, PayPalTxn.created_utc>now-60*60*24*7).count(),
        "receivables_outstanding_cents": g.db.query(func.sum(User.negative_balance_cents)).filter(User.is_deleted==False, or_(User.is_banned == 0, and_(User.is_banned > 0, User.unban_utc > 0))).scalar(),
        "cents_written_off":g.db.query(func.sum(User.negative_balance_cents)).filter(or_(User.is_deleted==True, User.unban_utc > 0)).scalar(),
        "coins_redeemed_last_24_hrs": g.db.query(User).filter(User.premium_expires_utc>now+60*60*24*6, User.premium_expires_utc < now+60*60*24*7).count(),
        "coins_redeemed_last_week": g.db.query(User).filter(User.premium_expires_utc>now, User.premium_expires_utc < now+60*60*24*7).count(),
        "coins_in_circulation": g.db.query(func.sum(User.coin_balance)).filter(User.is_deleted==False, or_(User.is_banned==0, and_(User.is_banned>0, User.unban_utc>0))).scalar(),
        "coins_vanished": g.db.query(func.sum(User.coin_balance)).filter(or_(User.is_deleted==True, and_(User.is_banned>0, User.unban_utc==0))).scalar(),
        "receivables_outstanding_cents": g.db.query(func.sum(User.negative_balance_cents)).filter(User.is_deleted==False, or_(User.is_banned == 0, and_(User.is_banned > 0, User.unban_utc > 0))).scalar(),
        "coins_sold_ytd":g.db.query(func.sum(PayPalTxn.coin_count)).filter(PayPalTxn.status==3, PayPalTxn.created_utc>midnight_year_start).scalar(),
        "revenue_usd_ytd":f"{revenue[0:-2]}.{revenue[-2:]}"
    }
    return render_template("admin/content_stats.html", v=v, title="Financial Statistics", data=data)


@app.route("/admin/vote_info", methods=["GET"])
@admin_level_required(4)
def admin_vote_info_get(v):

    if not request.args.get("link"):
        return render_template("admin/votes.html", v=v)

    thing = get_from_permalink(request.args.get("link"), v=v)

    if isinstance(thing, Submission):

        ups = g.db.query(Vote
                         ).options(joinedload(Vote.user)
                                   ).filter_by(submission_id=thing.id, vote_type=1
                                               ).order_by(Vote.creation_ip.asc()
                                                          ).all()

        downs = g.db.query(Vote
                           ).options(joinedload(Vote.user)
                                     ).filter_by(submission_id=thing.id, vote_type=-1
                                                 ).order_by(Vote.creation_ip.asc()
                                                            ).all()

    elif isinstance(thing, Comment):

        ups = g.db.query(CommentVote
                         ).options(joinedload(CommentVote.user)
                                   ).filter_by(comment_id=thing.id, vote_type=1
                                               ).order_by(CommentVote.creation_ip.asc()
                                                          ).all()

        downs = g.db.query(CommentVote
                           ).options(joinedload(CommentVote.user)
                                     ).filter_by(comment_id=thing.id, vote_type=-1
                                                 ).order_by(CommentVote.creation_ip.asc()
                                                            ).all()

    else:
        abort(400)

    return render_template("admin/votes.html",
                           v=v,
                           thing=thing,
                           ups=ups,
                           downs=downs,)


@app.route("/admin/alt_votes", methods=["GET"])
@admin_level_required(4)
def alt_votes_get(v):

    if not request.args.get("u1") or not request.args.get("u2"):
        return render_template("admin/alt_votes.html", v=v)

    u1 = request.args.get("u1")
    u2 = request.args.get("u2")

    if not u1 or not u2:
        return redirect("/admin/alt_votes")

    u1 = get_user(u1)
    u2 = get_user(u2)

    u1_post_ups = g.db.query(
        Vote.submission_id).filter_by(
        user_id=u1.id,
        vote_type=1).all()
    u1_post_downs = g.db.query(
        Vote.submission_id).filter_by(
        user_id=u1.id,
        vote_type=-1).all()
    u1_comment_ups = g.db.query(
        CommentVote.comment_id).filter_by(
        user_id=u1.id,
        vote_type=1).all()
    u1_comment_downs = g.db.query(
        CommentVote.comment_id).filter_by(
        user_id=u1.id,
        vote_type=-1).all()
    u2_post_ups = g.db.query(
        Vote.submission_id).filter_by(
        user_id=u2.id,
        vote_type=1).all()
    u2_post_downs = g.db.query(
        Vote.submission_id).filter_by(
        user_id=u2.id,
        vote_type=-1).all()
    u2_comment_ups = g.db.query(
        CommentVote.comment_id).filter_by(
        user_id=u2.id,
        vote_type=1).all()
    u2_comment_downs = g.db.query(
        CommentVote.comment_id).filter_by(
        user_id=u2.id,
        vote_type=-1).all()

    data = {}
    data['u1_only_post_ups'] = len(
        [x for x in u1_post_ups if x not in u2_post_ups])
    data['u2_only_post_ups'] = len(
        [x for x in u2_post_ups if x not in u1_post_ups])
    data['both_post_ups'] = len(list(set(u1_post_ups) & set(u2_post_ups)))

    data['u1_only_post_downs'] = len(
        [x for x in u1_post_downs if x not in u2_post_downs])
    data['u2_only_post_downs'] = len(
        [x for x in u2_post_downs if x not in u1_post_downs])
    data['both_post_downs'] = len(
        list(set(u1_post_downs) & set(u2_post_downs)))

    data['u1_only_comment_ups'] = len(
        [x for x in u1_comment_ups if x not in u2_comment_ups])
    data['u2_only_comment_ups'] = len(
        [x for x in u2_comment_ups if x not in u1_comment_ups])
    data['both_comment_ups'] = len(
        list(set(u1_comment_ups) & set(u2_comment_ups)))

    data['u1_only_comment_downs'] = len(
        [x for x in u1_comment_downs if x not in u2_comment_downs])
    data['u2_only_comment_downs'] = len(
        [x for x in u2_comment_downs if x not in u1_comment_downs])
    data['both_comment_downs'] = len(
        list(set(u1_comment_downs) & set(u2_comment_downs)))

    data['u1_post_ups_unique'] = 100 * \
        data['u1_only_post_ups'] // len(u1_post_ups) if u1_post_ups else 0
    data['u2_post_ups_unique'] = 100 * \
        data['u2_only_post_ups'] // len(u2_post_ups) if u2_post_ups else 0
    data['u1_post_downs_unique'] = 100 * \
        data['u1_only_post_downs'] // len(
            u1_post_downs) if u1_post_downs else 0
    data['u2_post_downs_unique'] = 100 * \
        data['u2_only_post_downs'] // len(
            u2_post_downs) if u2_post_downs else 0

    data['u1_comment_ups_unique'] = 100 * \
        data['u1_only_comment_ups'] // len(
            u1_comment_ups) if u1_comment_ups else 0
    data['u2_comment_ups_unique'] = 100 * \
        data['u2_only_comment_ups'] // len(
            u2_comment_ups) if u2_comment_ups else 0
    data['u1_comment_downs_unique'] = 100 * \
        data['u1_only_comment_downs'] // len(
            u1_comment_downs) if u1_comment_downs else 0
    data['u2_comment_downs_unique'] = 100 * \
        data['u2_only_comment_downs'] // len(
            u2_comment_downs) if u2_comment_downs else 0

    return render_template("admin/alt_votes.html",
                           u1=u1,
                           u2=u2,
                           v=v,
                           data=data
                           )


@app.route("/admin/link_accounts", methods=["POST"])
@admin_level_required(4)
@validate_formkey
def admin_link_accounts(v):

    u1 = int(request.form.get("u1"))
    u2 = int(request.form.get("u2"))

    new_alt = Alt(
        user1=u1, 
        user2=u2,
        is_manual=True
        )

    g.db.add(new_alt)
    g.db.commit()

    return redirect(f"/admin/alt_votes?u1={g.db.query(User).get(u1).username}&u2={g.db.query(User).get(u2).username}")


@app.route("/admin/<pagename>", methods=["GET"])
@admin_level_required(3)
def admin_tools(v, pagename):
    return render_template(f"admin/{pagename}.html", v=v)


@app.route("/admin/removed", methods=["GET"])
@admin_level_required(3)
def admin_removed(v):

    page = int(request.args.get("page", 1))

    ids = g.db.query(Submission.id).options(lazyload('*')).filter_by(is_banned=True).order_by(
        Submission.id.desc()).offset(25 * (page - 1)).limit(26).all()

    ids=[x[0] for x in ids]

    next_exists = len(ids) == 26

    ids = ids[0:25]

    posts = get_posts(ids, v=v)

    return render_template("admin/removed_posts.html",
                           v=v,
                           listing=posts,
                           page=page,
                           next_exists=next_exists
                           )

@app.route("/admin/gm", methods=["GET"])
@admin_level_required(3)
def admin_gm(v):
    
    username=request.args.get("user")

    include_banned=int(request.args.get("with_banned",0))

    if username:
        user=get_user(username)
        
        boards=user.boards_modded

        alts=user.alts
        main=user
        main_count=user.submissions.count() + user.comments.count()
        for alt in alts:

            if not alt.is_valid and not include_banned:
                continue

            count = alt.submissions.count() + alt.comments.count()
            if count > main_count:
                main_count=count
                main=alt

            for b in alt.boards_modded:
                if b not in boards:
                    boards.append(b)

           
        return render_template("admin/alt_gms.html",
            v=v,
            user=user,
            first=main,
            boards=boards
            )
    else:
        return render_template("admin/alt_gms.html",
            v=v)
    


@app.route("/admin/appdata", methods=["GET"])
@admin_level_required(4)
def admin_appdata(v):

    url=request.args.get("link")

    if url:

        thing = get_from_permalink(url, v=v)

        return render_template(
            "admin/app_data.html",
            v=v,
            thing=thing
            )

    else:
        return render_template(
            "admin/app_data.html",
            v=v)

@app.route("/admin/ban_analysis")
@admin_level_required(3)
def admin_ban_analysis(v):

    banned_accounts = g.db.query(User).filter(User.is_banned>0, User.unban_utc==0).all()

    uniques=set()

    seen_so_far=set()

    for user in banned_accounts:


        if user.id not in seen_so_far:

            print(f"Unique - @{user.username}")

            uniques.add(user.id)

        else:
            print(f"Repeat - @{user.username}")
            continue

        alts=user.alts
        print(f"{len(alts)} alts")

        for alt in user.alts:
            seen_so_far.add(alt.id)


    return str(len(uniques))


@app.route("/admin/paypaltxns", methods=["GET"])
@admin_level_required(4)
def admin_paypaltxns(v):

    page=int(request.args.get("page",1))
    user=request.args.get('user','')

    txns = g.db.query(PayPalTxn).filter(PayPalTxn.status!=1)

    if user:
        user=get_user(user)
        txns=txns.filter_by(user_id=user.id)


    txns=txns.order_by(PayPalTxn.created_utc.desc())

    txns = [x for x in txns.offset(100*(page-1)).limit(101).all()]

    next_exists=len(txns)==101
    txns=txns[0:100]

    return render_template(
        "single_txn.html", 
        v=v, 
        txns=txns, 
        next_exists=next_exists,
        page=page
        )

@app.route("/admin/domain/<domain_name>", methods=["GET"])
@admin_level_required(4)
def admin_domain_domain(domain_name, v):

    d_query=domain_name.replace("_","\_")
    domain=g.db.query(Domain).filter_by(domain=d_query).first()

    if not domain:
        domain=Domain(domain=domain_name)

    return render_template(
        "admin/manage_domain.html",
        v=v,
        domain_name=domain_name,
        domain=domain,
        reasons=REASONS
        )

@app.route("/admin/category", methods=["POST"])
@admin_level_required(4)
@validate_formkey
def admin_category_lock(v):

    board=get_guild(request.form.get("board"))

    cat_id=int(request.form.get("category"))

    sc=g.db.query(SubCategory).filter_by(id=cat_id).first()
    if not sc:
        abort(400)

    board.subcat_id=cat_id
    lock=bool(request.form.get("lock"))

    g.db.add(board)

    ma1=ModAction(
        board_id=board.id,
        user_id=v.id,
        kind="update_settings",
        note=f"category={sc.category.name} / {sc.name} | admin action"
        )
    g.db.add(ma1)

    if lock != board.is_locked_category:
        board.is_locked_category = lock
        ma2=ModAction(
            board_id=board.id,
            user_id=v.id,
            kind="update_settings",
            note=f"category_locked={lock} | admin action"
            )
        g.db.add(ma2)

    return redirect(f"{board.permalink}/mod/log")


@app.route("/admin/category", methods=["GET"])
@admin_level_required(4)
def admin_category_get(v):

    return render_template(
        "admin/category.html", 
        v=v,
        categories=CATEGORIES,
        b=get_board(request.args.get("guild"), graceful=True)
        )

@app.route("/admin/user_data", methods=["GET"])
@admin_level_required(5)
def admin_user_data_get(v):

    user=request.values.get("username")
    user=get_user(user, graceful=True)

    if not user:
        return render_template("admin/user_data.html", v=v)

    post_ids = [x[0] for x in g.db.query(Submission.id).filter_by(author_id=user.id).order_by(Submission.created_utc.desc()).all()]
    posts=get_posts(post_ids)

    comment_ids=[x[0] for x in g.db.query(Comment.id).filter_by(author_id=user.id).order_by(Comment.created_utc.desc()).all()]
    comments=get_comments(comment_ids)



    return jsonify(
        {
        "submissions":[x.json_admin for x in posts],
        "comments":[x.json_admin for x in comments],
        "user":user.json_admin
            }
        )
        
@app.route("/admin/image_purge", methods=["POST"])
@admin_level_required(5)
def admin_image_purge(v):
    
    url=request.form.get("url")

    parsed_url=urlparse(url)

    name=parsed_url.path.lstrip('/')

    try:
        print(name)
    except:
        pass


    aws.delete_file(name)

    return "", 204


@app.route("/admin/ip/<ipaddr>", methods=["GET"])
@admin_level_required(5)
def admin_ip_addr(ipaddr, v):

    pids=[x.id for x in g.db.query(Submission).filter_by(creation_ip=ipaddr).order_by(Submission.created_utc.desc()).all()]

    cids=[x.id for x in g.db.query(Comment).filter(Comment.creation_ip==ipaddr, Comment.parent_submission!=None).order_by(Comment.created_utc.desc()).all()]

    return render_template(
        "admin/ip.html",
        v=v,
        users=g.db.query(User).filter_by(creation_ip=ipaddr).order_by(User.created_utc.desc()).all(),
        listing=get_posts(pids) if pids else [],
        comments=get_comments(cids) if cids else [],
        standalone=True
        )

@app.route("/admin/test", methods=["GET"])
@admin_level_required(5)
def admin_test_ip(v):

    return f"IP: {request.remote_addr}; fwd: {request.headers.get('X-Forwarded-For')}"


@app.route("/admin/siege_count")
@admin_level_required(3)
def admin_siege_count(v):

    board=get_guild(request.args.get("board"))
    recent=int(request.args.get("days",0))

    now=int(time.time())

    cutoff=board.stored_subscriber_count//10 + min(recent, (now-board.created_utc)//(60*60*24))

    uids=g.db.query(Subscription.user_id).filter_by(is_active=True, board_id=board.id).all()
    uids=[x[0] for x in uids]

    can_siege=0
    total=0
    for uid in uids:
        posts=sum([x[0] for x in g.db.query(Submission.score_top).options(lazyload('*')).filter_by(author_id=uid).filter(Submission.created_utc>now-60*60*24*recent).all()])
        comments=sum([x[0] for x in g.db.query(Comment.score_top).options(lazyload('*')).filter_by(author_id=uid).filter(   Comment.created_utc>now-60*60*24*recent).all()])
        rep=posts+comments
        if rep>=cutoff:
            can_siege+=1
        total+=1
        print(f"{can_siege}/{total}")



    return jsonify(
        {
        "guild":f"+{board.name}",
        "requirement":cutoff,
        "eligible_users":can_siege
        }
        )


# @app.route('/admin/deploy', methods=["GET"])
# @admin_level_required(3)
# def admin_deploy(v):

#     def reload_function():
#         time.sleep(3)
#         subprocess.run(". ~/go.sh", shell=True)

#     thread=threading.Thread(target=reload_function, daemon=True)
#     thread.start()

#     return 'Reloading!'

# @app.route('/admin/test', methods=["GET"])
# @admin_level_required(3)
# def admin_test(v):


#     return "1"


@app.route("/admin/purge_guild_images/<boardname>", methods=["POST"])
@admin_level_required(5)
@validate_formkey
def admin_purge_guild_images(boardname, v):

    #Iterates through all posts in guild with thumbnail, and nukes thumbnails and i.ruqqus uploads

    board=get_guild(boardname)

    if not board.is_banned:
        return jsonify({"error":"This guild isn't banned"}), 409

    if board.has_profile:
        board.del_profile()

    if board.has_banner:
        board.del_banner()

    posts = g.db.query(Submission).options(lazyload('*')).filter_by(board_id=board.id, has_thumb=True)


    i=0

    for post in posts:
        i+=1
        aws.delete_file(urlparse(post.thumb_url).path.lstrip('/'))
        post.has_thumb=False

        if post.url and post.domain=="i.ruqqus.com":
            aws.delete_file(urlparse(post.url).path.lstrip('/'))

        g.db.add(post)

        if not i%100:
            g.db.commit()


    g.db.commit()

    return redirect(board.permalink)

@app.route("/admin/image_ban", methods=["POST"])
@admin_level_required(4)
@validate_formkey
def admin_image_ban(v):

    i=request.files['file']


    #make phash
    tempname = f"admin_image_ban_{v.username}_{int(time.time())}"

    i.save(tempname)

    h=imagehash.phash(IMAGE.open(tempname))
    h=hex2bin(str(h))

    #check db for existing
    badpic = g.db.query(BadPic).filter_by(
        phash=h
        ).first()

    remove(tempname)

    if badpic:
        return render_template("admin/image_ban.html", v=v, existing=badpic)

    new_bp=BadPic(
        phash=h,
        ban_reason=request.form.get("ban_reason"),
        ban_time=int(request.form.get("ban_length",0))
        )

    g.db.add(new_bp)
    g.db.commit()

    return render_template("admin/image_ban.html", v=v, success=True)