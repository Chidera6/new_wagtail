from django.core.paginator import InvalidPage, Paginator
from django.db import transaction
from django.db.models import Sum, functions
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.vary import vary_on_headers
from django.views.generic import ListView

from wagtail.admin import messages
from wagtail.admin.auth import any_permission_required, permission_required
from wagtail.admin.forms.search import SearchForm
from wagtail.admin.modal_workflow import render_modal_workflow
from wagtail.admin.views.generic import IndexView
from wagtail.contrib.search_promotions import forms, models
from wagtail.contrib.search_promotions.models import Query,SearchPromotion
from wagtail.log_actions import log
from wagtail.search.utils import normalise_query_string

decorators = [vary_on_headers, any_permission_required,permission_required]

class Index(IndexView, ListView):
    model = Query
    template_name = "wagtailsearchpromotions/index.html"
    results_template_name = "wagtailsearchpromotions/results.html"
    default_ordering = "query_string"
    any_permission_required = [
        "wagtailsearchpromotions.add_searchpromotion",
        "wagtailsearchpromotions.change_searchpromotion",
        "wagtailsearchpromotions.delete_searchpromotion",
    ]
    paginate_by = 20
    search_kwarg = "q"
    is_searchable = True
    
    @method_decorator(vary_on_headers("X-Requested-With"))
    def dispatch(self, request):
        return super().dispatch(request)
    
    def get_valid_orderings(self):
        return ["query_string", "-query_string", "views", "-views"]

    def get_queryset(self):
        queryset = self.model.objects.filter(editors_picks__isnull=False).distinct()
        ordering = self.get_ordering()

        if "views" in ordering:
            queryset = queryset.annotate(
                views=Sum("daily_hits__hits", distinct=True)
            ).distinct()   

        query_string = self.request.GET.get(self.search_kwarg, "")
        if query_string:
            queryset = queryset.filter(query_string__icontains=query_string)

        return queryset.order_by(ordering)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        context["queries"] = self.get_queryset()
        context["ordering"] = self.get_ordering()
        if self.request.headers.get("x-requested-with") == "XMLHttpRequest":
            self.template_name = self.results_template_name
        else:
            context["search_form"] = SearchForm(
            data={"q": self.search_query} if self.search_query else None,
            placeholder=_("Search promoted results"),
        )
            self.template_name = self.template_name
    
        return context



def save_searchpicks(query, new_query, searchpicks_formset):
    # Save
    if searchpicks_formset.is_valid():
        # Set sort_order
        for i, form in enumerate(searchpicks_formset.ordered_forms):
            form.instance.sort_order = i

            # Make sure the form is marked as changed so it gets saved with the new order
            form.has_changed = lambda: True

        # log deleted items before saving, otherwise we lose their IDs
        items_for_deletion = [
            form.instance
            for form in searchpicks_formset.deleted_forms
            if form.instance.pk
        ]
        with transaction.atomic():
            for search_pick in items_for_deletion:
                log(search_pick, "wagtail.delete")

            searchpicks_formset.save()

            for search_pick in searchpicks_formset.new_objects:
                log(search_pick, "wagtail.create")

            # If query was changed, move all search picks to the new query
            if query != new_query:
                searchpicks_formset.get_queryset().update(query=new_query)
                # log all items in the formset as having changed
                for search_pick, changed_fields in searchpicks_formset.changed_objects:
                    log(search_pick, "wagtail.edit")
            else:
                # only log objects with actual changes
                for search_pick, changed_fields in searchpicks_formset.changed_objects:
                    if changed_fields:
                        log(search_pick, "wagtail.edit")

        return True
    else:
        return False


@permission_required("wagtailsearchpromotions.add_searchpromotion")
def add(request):
    if request.method == "POST":
        #checks if its  a post request
        # Get query
        query_form = forms.QueryForm(request.POST)
        #gets the form
        if query_form.is_valid():
            #check if the form is valid
            query = Query.get(query_form["query_string"].value())
            #make  a query

            # Save search picks
            searchpicks_formset = forms.SearchPromotionsFormSet(
                request.POST, instance=query
            )
            if save_searchpicks(query, query, searchpicks_formset):
                #call the save search picks function
                for search_pick in searchpicks_formset.new_objects:
                    log(search_pick, "wagtail.create")
                messages.success(
                    request,
                    _("Editor's picks for '%(query)s' created.") % {"query": query},
                    buttons=[
                        messages.button(
                            reverse("wagtailsearchpromotions:edit", args=(query.id,)),
                            _("Edit"),
                        )
                    ],
                )
                return redirect("wagtailsearchpromotions:index")
            else:
                if len(searchpicks_formset.non_form_errors()):
                    # formset level error (e.g. no forms submitted)
                    messages.error(
                        request,
                        " ".join(
                            error for error in searchpicks_formset.non_form_errors()
                        ),
                    )
                else:
                    # specific errors will be displayed within form fields
                    messages.error(
                        request,
                        _("Recommendations have not been created due to errors"),
                    )
        else:
            searchpicks_formset = forms.SearchPromotionsFormSet()
    else:
        query_form = forms.QueryForm()
        searchpicks_formset = forms.SearchPromotionsFormSet()

    return TemplateResponse(
        request,
        "wagtailsearchpromotions/add.html",
        {
            "query_form": query_form,
            "searchpicks_formset": searchpicks_formset,
            "form_media": query_form.media + searchpicks_formset.media,
        },
    )

@permission_required("wagtailsearchpromotions.change_searchpromotion")
def edit(request, query_id):
    query = get_object_or_404(Query, id=query_id)

    if request.method == "POST":
        # Get query
        query_form = forms.QueryForm(request.POST)
        # and the recommendations
        searchpicks_formset = forms.SearchPromotionsFormSet(
            request.POST, instance=query
        )

        if query_form.is_valid():
            new_query = Query.get(query_form["query_string"].value())

            # Save search picks
            if save_searchpicks(query, new_query, searchpicks_formset):
                messages.success(
                    request,
                    _("Editor's picks for '%(query)s' updated.") % {"query": new_query},
                    buttons=[
                        messages.button(
                            reverse("wagtailsearchpromotions:edit", args=(query.id,)),
                            _("Edit"),
                        )
                    ],
                )
                return redirect("wagtailsearchpromotions:index")
            else:
                if len(searchpicks_formset.non_form_errors()):
                    messages.error(
                        request,
                        " ".join(
                            error for error in searchpicks_formset.non_form_errors()
                        ),
                    )
                    # formset level error (e.g. no forms submitted)
                else:
                    messages.error(
                        request, _("Recommendations have not been saved due to errors")
                    )
                    # specific errors will be displayed within form fields

    else:
        query_form = forms.QueryForm(initial={"query_string": query.query_string})
        searchpicks_formset = forms.SearchPromotionsFormSet(instance=query)

    return TemplateResponse(
        request,
        "wagtailsearchpromotions/edit.html",
        {
            "query_form": query_form,
            "searchpicks_formset": searchpicks_formset,
            "query": query,
            "form_media": query_form.media + searchpicks_formset.media,
        },
    )


@permission_required("wagtailsearchpromotions.delete_searchpromotion")
def delete(request, query_id):
    query = get_object_or_404(Query, id=query_id)

    if request.method == "POST":
        editors_picks = query.editors_picks.all()
        with transaction.atomic():
            for search_pick in editors_picks:
                log(search_pick, "wagtail.delete")
            editors_picks.delete()
        messages.success(request, _("Editor's picks deleted."))
        return redirect("wagtailsearchpromotions:index")

    return TemplateResponse(
        request,
        "wagtailsearchpromotions/confirm_delete.html",
        {
            "query": query,
        },
    )


def chooser(request, get_results=False):
    # Get most popular queries
    queries = models.Query.get_most_popular()

    # If searching, filter results by query string
    if "q" in request.GET:
        searchform = SearchForm(request.GET)
        if searchform.is_valid():
            query_string = searchform.cleaned_data["q"]
            queries = queries.filter(
                query_string__icontains=normalise_query_string(query_string)
            )
    else:
        searchform = SearchForm()

    paginator = Paginator(queries, per_page=10)
    try:
        queries = paginator.page(request.GET.get("p", 1))
    except InvalidPage:
        raise Http404

    # Render
    if get_results:
        return TemplateResponse(
            request,
            "wagtailsearchpromotions/queries/chooser/results.html",
            {
                "queries": queries,
            },
        )
    else:
        return render_modal_workflow(
            request,
            "wagtailsearchpromotions/queries/chooser/chooser.html",
            None,
            {
                "queries": queries,
                "searchform": searchform,
            },
            json_data={"step": "chooser"},
        )


def chooserresults(request):
    return chooser(request, get_results=True)
