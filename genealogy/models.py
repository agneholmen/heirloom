from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import OuterRef, PositiveSmallIntegerField, Q, Subquery
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from datetime import date
from easy_thumbnails.files import get_thumbnailer
from itertools import chain

from .date_functions import extract_year

def users_file_location(instance, filename):
    date_string = date.today().strftime("%Y/%m/%d")
    return f"users/{instance.user.username}/{date_string}/{filename}"


class Tree(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )
    upload_date = models.DateField(default=date.today)
    name = models.CharField(
        max_length=100
    )
    description = models.CharField(
        blank=True,
        max_length=200
    )
    gedcom_file = models.FileField(
        upload_to=users_file_location,
        blank=True
    )
    private = models.BooleanField(default=False)

    def clean(self):
        if Tree.objects.filter(user=self.user, name=self.name).exclude(id=self.id).exists():
            raise ValidationError({'name': 'A tree with that name already exists.'})
        if not self.name:
            raise ValidationError({'name': 'A tree needs to have a name.'})

    def __str__(self) -> str:
        return self.name

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'name'], name='User and name combination')
        ]

class Person(models.Model):
    SEX_CHOICES = (
        ("M", "Male"),
        ("F", "Female"),
        ("U", "Unknown"),
    )

    indi_id = models.CharField(max_length=20, blank=True)
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE, related_name="persons")
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    sex = models.CharField(max_length=10, choices=SEX_CHOICES, default="U")
    death_cause = models.CharField(max_length=100, blank=True)
    alive = models.BooleanField(default=False)
    added = models.DateField(auto_now_add=True)
    last_updated = models.DateField(auto_now=True)
    profile_image = models.ForeignKey('Image', on_delete=models.SET_NULL, null=True, blank=True, related_name="profile_of")

    class Meta:
        ordering = ['last_name']

    def get_father(self):
        try:
            family = Family.objects.filter(children__person=self).first()
            if family and family.husband_id == self.id:
                return None
            return family.husband
        except:
            return None
        
    def get_mother(self):
        try:
            family = Family.objects.filter(children__person=self).first()
            if family and family.wife_id == self.id:
                return None
            return family.wife
        except:
            return None

    def get_name_years(self):
        string = self.get_name()

        string += f" {self.get_years()}"

        return string.strip()
    
    def get_name(self):
        string = ""
        if self.first_name:
            string += self.first_name
        if self.last_name:
            string += f" {self.last_name}"

        return string.strip()
    
    def get_years(self):
        birth_year = self.get_birth_year()
        death_year = self.get_death_year()

        if birth_year and death_year:
            return f"({birth_year} - {death_year})"
        elif birth_year:
            return f"({birth_year} - )"
        elif death_year:
            return f"( - {death_year})"
        else:
            return ""
    
    # Birth
    def get_birth_date(self):
        birth_event = self.get_birth_event()
        if birth_event:
            return birth_event.date
        else:
            return None
        
    def get_birth_year(self):
        birth_event = self.get_birth_event()
        if birth_event:
            return birth_event.year
        else:
            return None

    def get_birth_place(self):
        birth_event = self.get_birth_event()
        if birth_event:
            return birth_event.place
        else:
            return None
       
    def get_birth_event(self):
        try:
            return Event.objects.get(person=self, event_type='birth')
        except Event.DoesNotExist:
            return None
        except Event.MultipleObjectsReturned:
            print(f"Multiple birth events found for {self}. Returning None.")
            return None
        
    def has_birth_event(self):
        return Event.objects.filter(person=self, event_type='birth').exists()
    
    # Death  
    def get_death_year(self):
        death_event = self.get_death_event()
        if death_event:
            return death_event.year
        else:
            return None
         
    def get_death_place(self):
        death_event = self.get_death_event()
        if death_event:
            return death_event.place
        else:
            return None

    def get_death_date(self):
        death_event = self.get_death_event()
        if death_event:
            return death_event.date
        else:
            return None
        
    def get_death_event(self):
        try:
            return Event.objects.get(person=self, event_type='death')
        except Event.DoesNotExist:
            return None
        except Event.MultipleObjectsReturned:
            print(f"Multiple death events found for {self}. Returning None.")
            return None
        
    def has_death_event(self):
        return Event.objects.filter(person=self, event_type='death').exists()

    def _event_sources_payload(self, event_obj):
        return [
            {
                'id': source.id,
                'title': source.title,
                'source_type': source.source_type,
                'source_type_display': source.get_source_type_display(),
                'url': source.web_link,
            }
            for source in event_obj.sources.all()
        ]

    def _event_images_payload(self, event_obj):
        return [
            {
                'id': image.id,
                'title': image.title,
                'url': image.image.url if image.image else '',
            }
            for image in event_obj.images.all()
        ]

    def get_data_quality_warnings(self):
        warnings = []

        birth_events = Event.objects.filter(person=self, event_type='birth').order_by('id')
        death_events = Event.objects.filter(person=self, event_type='death').order_by('id')

        if birth_events.count() > 1:
            warnings.append({
                'code': 'multiple_birth_events',
                'message': 'This person has multiple birth events. Only one should exist.',
            })

        if death_events.count() > 1:
            warnings.append({
                'code': 'multiple_death_events',
                'message': 'This person has multiple death events. Only one should exist.',
            })

        birth_event = birth_events.first()
        death_event = death_events.first()
        birth_year = birth_event.year if birth_event else None
        death_year = death_event.year if death_event else None

        if birth_year and death_year and birth_year > death_year:
            warnings.append({
                'code': 'birth_after_death',
                'message': f'Birth year ({birth_year}) is after death year ({death_year}).',
            })

        own_events = Event.objects.filter(person=self).exclude(event_type__in=['birth', 'death'])
        for event in own_events:
            if not event.year:
                continue
            if birth_year and event.year < birth_year:
                warnings.append({
                    'code': 'event_before_birth',
                    'message': f'{event.get_event_type_display()} ({event.year}) is before birth year ({birth_year}).',
                })
            if death_year and event.year > death_year:
                warnings.append({
                    'code': 'event_after_death',
                    'message': f'{event.get_event_type_display()} ({event.year}) is after death year ({death_year}).',
                })

        parent_family = Family.objects.filter(children__person=self).first()
        if parent_family and (parent_family.husband_id == self.id or parent_family.wife_id == self.id):
            warnings.append({
                'code': 'self_as_parent',
                'message': 'This person is linked as their own parent.',
            })

        if parent_family and birth_year:
            parent_checks = [
                ('father', parent_family.husband),
                ('mother', parent_family.wife),
            ]
            for parent_label, parent in parent_checks:
                if not parent:
                    continue

                parent_birth = parent.get_birth_event()
                parent_death = parent.get_death_event()

                if parent_birth and parent_birth.year and parent_birth.year > birth_year:
                    warnings.append({
                        'code': f'{parent_label}_born_after_child',
                        'message': f'The {parent_label} appears to be born after this person.',
                    })

                if parent_death and parent_death.year and parent_death.year < birth_year:
                    warnings.append({
                        'code': f'{parent_label}_died_before_child_birth',
                        'message': f'The {parent_label} appears to have died before this person was born.',
                    })

        if birth_year or death_year:
            families = Family.objects.filter(Q(husband=self) | Q(wife=self))
            children = Child.objects.filter(family__in=families).select_related('person')
            seen_children = set()
            for child in children:
                if child.person_id in seen_children:
                    continue
                seen_children.add(child.person_id)

                child_birth = child.person.get_birth_event()
                if not child_birth or not child_birth.year:
                    continue

                if birth_year and child_birth.year < birth_year:
                    warnings.append({
                        'code': 'child_born_before_parent',
                        'message': f'Child {child.person.get_name()} appears born before this person.',
                    })

                if death_year and child_birth.year > death_year:
                    warnings.append({
                        'code': 'child_born_after_parent_death',
                        'message': f'Child {child.person.get_name()} appears born after this person\'s death.',
                    })

        return warnings

    # Serializer data method
    def get_details_data(self):
        data = {
            'father': None,
            'mother': None,
            'siblings': [],
            'half_siblings': [],
            'families': [],
        }


        timeline_events = []
        timeline_events_no_year = []

        birth_event = self.get_birth_event()
        birth_year = birth_event.year if birth_event else None
        if birth_event:
            data['birth'] = {
                'date': birth_event.date,
                'year': birth_event.year,
                'place': birth_event.place,
            }

        death_event = self.get_death_event()
        death_year = death_event.year if death_event else None
        if death_event:
            data['death'] = {
                'date': death_event.date,
                'year': death_event.year,
                'place': death_event.place,
            }

        if birth_year and death_year:
            data['years'] = f"({birth_year} - {death_year})"
        elif birth_year:
            data['years'] = f"({birth_year} - )"
        elif death_year:
            data['years'] = f"( - {death_year})"
        else:
            data['years'] = ""

        mother = self.get_mother()
        father = self.get_father()

        if father:
            data['father'] = {
                'id': father.id,
                'full_name': father.get_name_years(),
            }
            f_death = father.get_death_event()
            if not (f_death and death_year and f_death.year and f_death.year > death_year) and f_death and f_death.year:
                timeline_events.append(
                    {
                        'year': f_death.year, 
                        'description': f_death.description, 
                        'date': f_death.date,
                        'event_type': 'death', 
                        'event_type_full': "Death of father",
                        'place': f_death.place,
                        'person_id': father.id,
                        'person_name': father.get_name(),
                        'model_type': 'relative',
                    }
                )
        if mother:
            data['mother'] = {
                'id': mother.id,
                'full_name': mother.get_name_years(),
            }
            m_death = mother.get_death_event()
            if not (m_death and death_year and m_death.year and m_death.year > death_year) and m_death and m_death.year:
                timeline_events.append(
                    {
                        'year': m_death.year,
                        'description': "", 
                        'date': m_death.date,
                        'event_type': 'death', 
                        'event_type_full': "Death of mother",
                        'place': m_death.place,
                        'person_id': mother.id,
                        'person_name': mother.get_name(),
                        'model_type': 'relative',
                    }
                )

        birth_year_subquery = Event.objects.filter(person=OuterRef('person'), event_type='birth').values('year')[:1]

        siblings = None
        half_siblings = None
        families = []
        children_objects = Child.objects.filter(person=self)
        if children_objects:
            siblings = Child.objects.filter(family=children_objects[0].family).exclude(id=children_objects[0].id).annotate(birth_year=Subquery(birth_year_subquery, output_field=PositiveSmallIntegerField())).order_by('birth_year')

            half_sibling_queries = Q()
            if father is not None:
                half_sibling_queries |= Q(husband=father) & ~Q(wife=mother)
            if mother is not None:
                half_sibling_queries |= Q(wife=mother) & ~Q(husband=father)

            half_sibling_families = Family.objects.filter(half_sibling_queries)
            half_siblings = Child.objects.filter(family__in=half_sibling_families).exclude(person=self).annotate(birth_year=Subquery(birth_year_subquery, output_field=PositiveSmallIntegerField())).order_by('birth_year')

        if siblings:
            for s in siblings:
                data['siblings'].append({
                    'id': s.person.id,
                    'full_name': s.person.get_name_years(),
            })
        if half_siblings:
            for h in half_siblings:
                data['half_siblings'].append({
                    'id': h.person.id,
                    'full_name': h.person.get_name_years(),
                })

        family_objects = Family.objects.filter(Q(husband=self) | Q(wife=self))
        if family_objects:
            for f in family_objects:
                family = {
                    'id': f.id,
                    'partner': {},
                    'children': []
                }

                partner = None
                if f.husband == self and f.wife:
                    partner = f.wife
                if f.wife == self and f.husband:
                    partner = f.husband

                if partner:
                    family['partner']['id'] = partner.id
                    family['partner']['full_name'] = partner.get_name_years()
                
                    p_death = partner.get_death_event()
                    if p_death and death_year and p_death.year and p_death.year < death_year:
                        timeline_events.append(
                            {
                                'year': p_death.year, 
                                'description': p_death.description, 
                                'date': p_death.date,
                                'event_type': 'death', 
                                'event_type_full': f"Death of {'husband' if partner.sex == 'M' else 'wife' if partner.sex == 'F' else 'partner'}",
                                'place': p_death.place,
                                'person_id': partner.id,
                                'person_name': partner.get_name(),
                                'model_type': 'relative',
                            }
                        )

                children = Child.objects.filter(family=f).annotate(birth_year=Subquery(birth_year_subquery, output_field=PositiveSmallIntegerField())).order_by('birth_year')
                for child in children:
                    family['children'].append({
                        'id': child.person.id,
                        'full_name': child.person.get_name_years(),
                    })
                    c_birth = child.person.get_birth_event()
                    c_death = child.person.get_death_event()
                    if c_birth and c_birth.year:
                        timeline_events.append(
                            {
                                'year': c_birth.year, 
                                'description': c_birth.description, 
                                'date': c_birth.date,
                                'event_type': 'birth', 
                                'event_type_full': f"Birth of {'son' if child.person.sex == 'M' else 'daughter' if child.person.sex == 'F' else 'child'}",
                                'place': c_birth.place, 
                                'person_id': child.person.id,
                                'person_name': child.person.get_name(),
                                'model_type': 'relative',
                            }
                        )
                    if c_death and death_year and c_death.year and c_death.year < death_year:
                        timeline_events.append(
                            {
                                'year': c_death.year, 
                                'description': c_death.description, 
                                'date': c_death.date,
                                'event_type': 'birth', 
                                'event_type_full': f"Death of {'son' if child.person.sex == 'M' else 'daughter' if child.person.sex == 'F' else 'child'}",
                                'place': c_death.place, 
                                'person_id': child.person.id,
                                'person_name': child.person.get_name(),
                                'model_type': 'relative',
                            }
                        )

                families.append(family)

            data['families'] = families

            family_events = FamilyEvent.objects.filter(family__in=family_objects).order_by('year')

            for e in family_events:
                new_event = {
                    'year': e.year, 
                    'description': e.description, 
                    'date': e.date,
                    'event_type': e.event_type, 
                    'event_type_full': e.get_event_type_display(),
                    'family_id': e.family_id,
                    'person_id': e.family.husband.id if e.family.wife == self else e.family.wife.id,
                    'person_name': e.family.husband.get_name() if e.family.wife == self else e.family.wife.get_name(),
                    'sources': self._event_sources_payload(e),
                    'images': self._event_images_payload(e),
                    'place': e.place, 
                    'id': e.id, 
                    'model_type': 'family'
                }
                if new_event['year']:
                    timeline_events.append(new_event)
                else:
                    timeline_events_no_year.append(new_event)

        for s in chain(siblings or [], half_siblings or []):
            s_birth = s.person.get_birth_event()
            s_death = s.person.get_death_event()
            if s_birth and birth_year and s_birth.year and s_birth.year > birth_year and not (death_year and s_birth.year > death_year):
                timeline_events.append(
                    {
                        'year': s_birth.year, 
                        'description': "", 
                        'date': s_birth.date,
                        'event_type': 'birth', 
                        'event_type_full': f"Birth of {'brother' if s.person.sex == 'M' else 'sister' if s.person.sex == 'F' else 'sibling'}",
                        'place': s_birth.place, 
                        'person_id': s.person.id,
                        'person_name': s.person.get_name(),
                        'model_type': 'relative',
                    }
                )
            if s_death and death_year and s_death.year and s_death.year < death_year and not (birth_year and s_death.year < birth_year):
                timeline_events.append(
                    {
                        'year': s_death.year, 
                        'description': "", 
                        'date': s_death.date,
                        'event_type': 'death', 
                        'event_type_full': f"Death of {'brother' if s.person.sex == 'M' else 'sister' if s.person.sex == 'F' else 'sibling'}",
                        'place': s_death.place,
                        'person_id': s.person.id,
                        'person_name': s.person.get_name(),
                        'model_type': 'relative',
                    }
                )

        events = Event.objects.filter(person=self).order_by('year')
        for e in events:
            if e.event_type not in ['birth', 'death']:
                new_event = {
                        'year': e.year, 
                        'description': e.description,
                        'date': e.date,
                        'event_type': e.event_type, 
                        'event_type_full': e.get_event_type_display(),
                        'sources': self._event_sources_payload(e),
                        'images': self._event_images_payload(e),
                        'place': e.place, 
                        'id': e.id, 
                        'model_type': 'basic'
                    }
                if new_event['year']:
                    timeline_events.append(new_event)
                else:
                    timeline_events_no_year.append(new_event)

        timeline_events.sort(key=lambda x: x['year'])

        if birth_event:
            timeline_events.insert(0, 
                               {'year': birth_year, 
                                'date': birth_event.date,
                                'description': birth_event.description, 
                                'event_type': 'birth',
                                'event_type_full': 'Birth', 
                                'sources': self._event_sources_payload(birth_event),
                                'images': self._event_images_payload(birth_event),
                                'place': birth_event.place, 
                                'id': birth_event.id, 
                                'model_type': 'basic'
                                }
                            )
            
        if death_event:
            timeline_events.append(
                {
                    'year': death_year, 
                    'date': death_event.date,
                    'description': death_event.description, 
                    'event_type': 'death', 
                    'event_type_full': 'Death',
                    'sources': self._event_sources_payload(death_event),
                    'images': self._event_images_payload(death_event),
                    'place': death_event.place, 
                    'id': death_event.id, 
                    'model_type': 'basic'
                }
            )

        timeline_events.extend(timeline_events_no_year)

        data['events'] = timeline_events
        data['quality_warnings'] = self.get_data_quality_warnings()

        return data

    def __str__(self):
        return " ".join(filter(None, [self.first_name, self.last_name]))
    

class Family(models.Model):
    family_id = models.CharField(max_length=20, blank=True)
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE)
    husband = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        related_name="families_as_husband",
        null=True,
        blank=True
    )
    wife = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        related_name="families_as_wife",
        null=True,
        blank=True
    )

    def clean(self):
        if self.husband and self.husband.tree != self.tree:
            raise ValidationError("Husband must belong to the same tree as the family.")
        if self.wife and self.wife.tree != self.tree:
            raise ValidationError("Wife must belong to the same tree as the family.")
    
    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        if self.husband and self.wife:
            return f"{self.husband} and {self.wife}"
        elif self.husband:
            return f"{self.husband} and unknown mother"
        else:
            return f"{self.wife} and unknown father"

    class Meta:
        verbose_name_plural = "Families"

class Child(models.Model):
    RELATIONS = (
        ("A", "Adopted"),
        ("B", "Biological"),
        ("F", "Foster"),
        ("U", "Unknown"),
    )

    family = models.ForeignKey(
        Family,
        on_delete=models.CASCADE,
        related_name="children"                   
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE
    )
    relation = models.CharField(
        choices=RELATIONS,
        max_length=1,
        default="B"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['family', 'person'], name='Person and family combination')
        ]
        verbose_name_plural = "Children"


class Event(models.Model):
    EVENT_TYPES = [
        ('baptism', 'Baptism'),
        ('confirmation', 'Confirmation'),
        ('cremation', 'Cremation'),
        ('emigration', 'Emigration'),
        ('funeral', 'Funeral'),
        ('graduation', 'Graduation'),
        ('immigration', 'Immigration'),
        ('residence', 'Place of Residence'),
        ('birth', 'Birth'),
        ('death', 'Death'),
        # Add other event types as needed
    ]

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    date = models.CharField(max_length=100, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    place = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    sources = models.ManyToManyField('Source', blank=True, related_name='person_events')
    images = models.ManyToManyField('Image', blank=True, related_name='person_events')

    def clean(self):
        try:
            birth_event = Event.objects.get(person=self.person, event_type='birth')
            if birth_event.pk != self.pk:
                raise ValidationError("A person can only have one birth event.")
        except:
            pass
        try:
            death_event = Event.objects.get(person=self.person, event_type='death')
            if death_event.pk != self.pk:
                raise ValidationError("A person can only have one death event.")
        except:
            pass
        
    @staticmethod
    def get_or_new(person, event_type):
        try:
            return Event.objects.get(person=person, event_type=event_type)
        except:
            return Event(person=person, event_type=event_type)

    def save(self, *args, **kwargs):
        if self.date:
            self.year = extract_year(self.date)
        elif self.year:
            self.year = None

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_event_type_display()} for {self.person}"
    

class FamilyEvent(models.Model):
    EVENT_TYPES = [
        ('banns', 'Banns'),
        ('divorce', 'Divorce'),
        ('engagement', 'Engagement'),
        ('marriage', 'Marriage'),
    ]

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="family_events")
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    date = models.CharField(max_length=100, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    place = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True, null=True)
    sources = models.ManyToManyField('Source', blank=True, related_name='family_events')
    images = models.ManyToManyField('Image', blank=True, related_name='family_events')

    def save(self, *args, **kwargs):
        if self.date:
            self.year = extract_year(self.date)
        elif self.year:
            self.year = None

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_event_type_display()} for {self.family}"


class Archive(models.Model):
    title = models.CharField(max_length=100)
    archive_id = models.CharField(max_length=20, blank=True)
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE)
    publisher = models.CharField(max_length=100, blank=True)
    author = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=1000, blank=True)
    web_link = models.CharField(max_length=500, blank=True)

    def __str__(self) -> str:
        return self.title
    
class Source(models.Model):
    SOURCE_TYPES = [
        ('book', 'Book/Volume'),
        ('parish', 'Parish Record'),
        ('civil', 'Civil Record'),
        ('website', 'Website'),
        ('photo', 'Photo/Image'),
        ('other', 'Other'),
    ]

    tree = models.ForeignKey(Tree, on_delete=models.CASCADE, related_name='sources', null=True, blank=True)
    archive = models.ForeignKey(Archive, on_delete=models.SET_NULL, null=True, blank=True, related_name='sources')
    title = models.CharField(max_length=100)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default='other')
    citation_text = models.TextField(blank=True)
    web_link = models.CharField(max_length=500, blank=True)
    description = models.CharField(max_length=1000, blank=True)
    publishing_date = models.CharField(max_length=50, blank=True)

    def __str__(self) -> str:
        return self.title
    
class Image(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tree = models.ForeignKey(Tree, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created = models.DateField(auto_now_add=True)
    slug = models.SlugField(max_length=200, blank=True)
    image = models.ImageField(upload_to=users_file_location)
    private = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['-created']

class ImagePerson(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    image = models.ForeignKey(Image, on_delete=models.CASCADE)

class ImageComment(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    image = models.ForeignKey(Image, on_delete=models.CASCADE)
    comment = models.TextField(blank=False)
    commented_at = models.DateTimeField(auto_now_add=True)

class ImageLike(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    image = models.ForeignKey(Image, on_delete=models.CASCADE)
    liked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'image'], name='unique_user_image_like')
        ]

# Handle cleanup of family, so there are no families with only one person and no children
# or families with only children
@receiver(pre_delete, sender=Person)
def handle_family_cleanup(sender, instance, **kwargs):
    families = Family.objects.filter(models.Q(husband=instance) | models.Q(wife=instance))
    for family in families:
        has_children = Child.objects.filter(family=family).exists()
        # Family with only one person and no children
        if not has_children:
            family.delete()
            continue
        # Family with children and only one parent
        if (family.husband == instance and not family.wife) or (family.wife == instance and not family.husband):
            family.delete()
            continue
        # Prevent partner from having multiple single parent families
        else:
            if family.husband == instance:
                partner_single_parent_families = Family.objects.filter(models.Q(husband=None) & models.Q(wife=family.wife))
            else:
                partner_single_parent_families = Family.objects.filter(models.Q(husband=family.husband) & models.Q(wife=None))

            if partner_single_parent_families:
                children = Child.objects.filter(family=family)
                for child in children:
                    old_family = Family.objects.get(id=partner_single_parent_families[0].id)
                    child.family = old_family
                    child.save()


    children = Child.objects.filter(person=instance)
    for child in children:
        if ((child.family.husband and not child.family.wife) or (child.family.wife and not child.family.husband)) and child.family.children.count() == 1:
            child.family.delete()

# Removes the GEDCOM file when you remove a Tree instance
@receiver(post_delete, sender=Tree)
def tree_post_delete_handler(sender, **kwargs):
    tree = kwargs['instance']
    if hasattr(tree, 'gedcome_file'):
        storage, path = tree.gedcom_file.storage, tree.gedcom_file.path
        storage.delete(path)

# Removes the Image file when you remove an Image instance
@receiver(post_delete, sender=Image)
def image_post_delete_handler(sender, **kwargs):
    image = kwargs['instance']
    if hasattr(image, 'image'):
        storage, path = image.image.storage, image.image.path
        storage.delete(path)

        thumbnailer = get_thumbnailer(image.image)
        thumbnailer.delete_thumbnails()