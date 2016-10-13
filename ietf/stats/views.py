import datetime, itertools, json, calendar

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import reverse as urlreverse
from django.http import HttpResponseRedirect

import dateutil.relativedelta

from ietf.review.utils import extract_review_request_data, aggregate_review_request_stats, ReviewRequestData
from ietf.group.models import Role, Group
from ietf.person.models import Person
from ietf.name.models import ReviewRequestStateName, ReviewResultName
from ietf.ietfauth.utils import has_role

def stats_index(request):
    return render(request, "stats/index.html")

@login_required
def review_stats(request, stats_type=None, acronym=None):
    # This view is a bit complex because we want to show a bunch of
    # tables with various filtering options, and both a team overview
    # and a reviewers-within-team overview - and a time series chart.
    # And in order to make the UI quick to navigate, we're not using
    # one big form but instead presenting a bunch of immediate
    # actions, with a URL scheme where the most common options (level
    # and statistics type) are incorporated directly into the URL to
    # be a bit nicer.

    def build_review_stats_url(stats_type_override=Ellipsis, acronym_override=Ellipsis, get_overrides={}):
        kwargs = {
            "stats_type": stats_type if stats_type_override is Ellipsis else stats_type_override,
        }
        acr = acronym if acronym_override is Ellipsis else acronym_override
        if acr:
            kwargs["acronym"] = acr

        base_url = urlreverse(review_stats, kwargs=kwargs)
        query_part = u""

        if request.GET or get_overrides:
            d = request.GET.copy()
            for k, v in get_overrides.iteritems():
                if type(v) in (list, tuple):
                    if not v:
                        if k in d:
                            del d[k]
                    else:
                        d.setlist(k, v)
                else:
                    if v is None or v == u"":
                        if k in d:
                            del d[k]
                    else:
                        d[k] = v

            if d:
                query_part = u"?" + d.urlencode()
            
        return base_url + query_part

    def get_from_selection(get_parameter, possible_choices):
        val = request.GET.get(get_parameter)
        for slug, label, url in possible_choices:
            if slug == val:
                return slug
        return None

    # which overview - team or reviewer
    if acronym:
        level = "reviewer"
    else:
        level = "team"

    # statistics type - one of the tables or the chart
    possible_stats_types = [
        ("completion", "Completion status"),
        ("results", "Review results"),
        ("states", "Request states"),
    ]

    if level == "team":
        possible_stats_types.append(("time", "Changes over time"))

    possible_stats_types = [ (slug, label, build_review_stats_url(stats_type_override=slug))
                             for slug, label in possible_stats_types ]

    if not stats_type:
        return HttpResponseRedirect(build_review_stats_url(stats_type_override=possible_stats_types[0][0]))

    # what to count
    possible_count_choices = [
        ("", "Review requests"),
        ("pages", "Reviewed pages"),
    ]

    possible_count_choices = [ (slug, label, build_review_stats_url(get_overrides={ "count": slug })) for slug, label in possible_count_choices ]

    count = get_from_selection("count", possible_count_choices) or ""

    # time range
    def parse_date(s):
        if not s:
            return None
        try:
            return datetime.datetime.strptime(s.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    today = datetime.date.today()
    from_date = parse_date(request.GET.get("from")) or today - dateutil.relativedelta.relativedelta(years=1)
    to_date = parse_date(request.GET.get("to")) or today

    from_time = datetime.datetime.combine(from_date, datetime.time.min)
    to_time = datetime.datetime.combine(to_date, datetime.time.max)

    # teams/reviewers
    teams = list(Group.objects.exclude(reviewrequest=None).distinct().order_by("name"))

    reviewer_filter_args = {}

    # - interlude: access control
    if has_role(request.user, ["Secretariat", "Area Director"]):
        pass
    else:
        secr_access = set()
        reviewer_only_access = set()

        for r in Role.objects.filter(person__user=request.user, name__in=["secr", "reviewer"], group__in=teams).distinct():
            if r.name_id == "secr":
                secr_access.add(r.group_id)
                reviewer_only_access.discard(r.group_id)
            elif r.name_id == "reviewer":
                if not r.group_id in secr_access:
                    reviewer_only_access.add(r.group_id)

        teams = [t for t in teams if t.pk in secr_access or t.pk in reviewer_only_access]

        for t in reviewer_only_access:
            reviewer_filter_args[t] = { "user": request.user }

    reviewers_for_team = None

    if level == "team":
        for t in teams:
            t.reviewer_stats_url = build_review_stats_url(acronym_override=t.acronym)

        query_teams = teams
        query_reviewers = None

        group_by_objs = { t.pk: t for t in query_teams }
        group_by_index = ReviewRequestData._fields.index("team")

    elif level == "reviewer":
        for t in teams:
            if t.acronym == acronym:
                reviewers_for_team = t
                break
        else:
            return HttpResponseRedirect(urlreverse(review_stats))

        query_reviewers = list(Person.objects.filter(
            email__reviewrequest__time__gte=from_time,
            email__reviewrequest__time__lte=to_time,
            email__reviewrequest__team=reviewers_for_team,
            **reviewer_filter_args.get(t.pk, {})
        ).distinct())
        query_reviewers.sort(key=lambda p: p.last_name())

        query_teams = [t]

        group_by_objs = { r.pk: r for r in query_reviewers }
        group_by_index = ReviewRequestData._fields.index("reviewer")

    # now aggregate the data
    possible_teams = possible_completion_types = possible_results = possible_states = None
    selected_team = selected_completion_type = selected_result = selected_state = None

    if stats_type == "time":
        possible_teams = [(t.acronym, t.acronym, build_review_stats_url(get_overrides={ "team": t.acronym })) for t in teams]
        selected_team = get_from_selection("team", possible_teams)
        query_teams = [t for t in query_teams if t.acronym == selected_team]

    extracted_data = extract_review_request_data(query_teams, query_reviewers, from_time, to_time, ordering=[level])

    if stats_type == "time":
        req_time_index = ReviewRequestData._fields.index("req_time")

        def time_key_fn(t):
            d = t[req_time_index].date()
            #d -= datetime.timedelta(days=d.weekday())
            d -= datetime.timedelta(days=d.day)
            return (t[group_by_index], d)

        found_results = set()
        found_states = set()
        aggrs = []
        for (group_pk, d), request_data_items in itertools.groupby(extracted_data, key=time_key_fn):
            aggr = aggregate_review_request_stats(request_data_items, count=count)

            aggrs.append((d, aggr))

            for slug in aggr["result"]:
                found_results.add(slug)
            for slug in aggr["state"]:
                found_states.add(slug)

        results = ReviewResultName.objects.filter(slug__in=found_results)
        states = ReviewRequestStateName.objects.filter(slug__in=found_states)

        # choice

        possible_completion_types = [
            ("completed_in_time", "Completed in time"),
            ("completed_late", "Completed late"),
            ("not_completed", "Not completed"),
            ("average_assignment_to_closure_days", "Avg. compl. days"),
        ]

        possible_completion_types = [
            (slug, label, build_review_stats_url(get_overrides={ "completion": slug, "result": None, "state": None }))
            for slug, label in possible_completion_types
        ]

        selected_completion_type = get_from_selection("completion", possible_completion_types)

        possible_results = [
            (r.slug, r.name, build_review_stats_url(get_overrides={ "completion": None, "result": r.slug, "state": None }))
            for r in results
        ]

        selected_result = get_from_selection("result", possible_results)
        
        possible_states = [
            (s.slug, s.name, build_review_stats_url(get_overrides={ "completion": None, "result": None, "state": s.slug }))
            for s in states
        ]

        selected_state = get_from_selection("state", possible_states)

        if not selected_completion_type and not selected_result and not selected_state:
            selected_completion_type = "completed_in_time"

        series_data = []
        for d, aggr in aggrs:
            v = 0
            if selected_completion_type is not None:
                v = aggr[selected_completion_type]
            elif selected_result is not None:
                v = aggr["result"][selected_result]
            elif selected_state is not None:
                v = aggr["state"][selected_state]

            series_data.append((calendar.timegm(d.timetuple()) * 1000, v))

        data = json.dumps([{
            "data": series_data
        }])

    else: # tabular data

        data = []

        found_results = set()
        found_states = set()
        for group_pk, request_data_items in itertools.groupby(extracted_data, key=lambda t: t[group_by_index]):
            aggr = aggregate_review_request_stats(request_data_items, count=count)

            # skip zero-valued rows
            if aggr["open"] == 0 and aggr["completed"] == 0 and aggr["not_completed"] == 0:
                continue

            aggr["obj"] = group_by_objs.get(group_pk)

            for slug in aggr["result"]:
                found_results.add(slug)
            for slug in aggr["state"]:
                found_states.add(slug)
            
            data.append(aggr)

        results = ReviewResultName.objects.filter(slug__in=found_results)
        states = ReviewRequestStateName.objects.filter(slug__in=found_states)

        # massage states/results breakdowns for template rendering
        for aggr in data:
            aggr["state_list"] = [aggr["state"].get(x.slug, 0) for x in states]
            aggr["result_list"] = [aggr["result"].get(x.slug, 0) for x in results]


    return render(request, 'stats/review_stats.html', {
        "team_level_url": build_review_stats_url(acronym_override=None),
        "level": level,
        "reviewers_for_team": reviewers_for_team,
        "teams": teams,
        "data": data,
        "states": states,
        "results": results,

        # options
        "possible_stats_types": possible_stats_types,
        "stats_type": stats_type,

        "possible_count_choices": possible_count_choices,
        "count": count,

        "from_date": from_date,
        "to_date": to_date,
        "today": today,

        # time options
        "possible_teams": possible_teams,
        "selected_team": selected_team,
        "possible_completion_types": possible_completion_types,
        "selected_completion_type": selected_completion_type,
        "possible_results": possible_results,
        "selected_result": selected_result,
        "possible_states": possible_states,
        "selected_state": selected_state,
    })
