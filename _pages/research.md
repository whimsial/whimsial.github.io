---
title: Research
nav: true
nav_order: 3
lead: >-
  I work in statistical genetics and genetics of complex traits, developing methods
  that pinpoint the disease-critical genes behind immune-mediated inflammatory
  and other complex diseases.
---

A cross-disciplinary path runs through these projects, from computational
mechanics, research software and computer science to my current focus on
statistical genetics and genetics of complex traits.

## Projects

<div class="projects">
{% for project in site.data.projects %}
{% if project.group_break and forloop.first == false %}<hr class="projects__sep">{% endif %}
{% if project.group_label %}<p class="projects__group-label">{{ project.group_label }}</p>{% endif %}
{% include project.html project=project %}
{% endfor %}
</div>
