# Generated by Django 2.2.5 on 2019-10-23 13:40

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='FileModel',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hashid', models.CharField(db_index=True, default='null', max_length=255, unique=True)),
                ('filename', models.CharField(default='null', max_length=255)),
                ('targetpvc', models.CharField(default='null', max_length=255)),
                ('targetpath', models.CharField(default='null', max_length=255)),
                ('status', models.PositiveSmallIntegerField(default=0)),
                ('uploadtime', models.CharField(default='null', max_length=255)),
            ],
        ),
    ]
