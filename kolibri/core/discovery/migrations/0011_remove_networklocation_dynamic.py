# -*- coding: utf-8 -*-
# Generated by Django 1.11.29 on 2023-08-08 17:50
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("discovery", "0010_set_location_type"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="networklocation",
            name="dynamic",
        ),
    ]
