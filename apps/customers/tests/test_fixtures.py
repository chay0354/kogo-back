"""
Test fixtures and helper functions for customer tests
"""
from datetime import date, timedelta
from apps.core.models import Branch, City
from apps.customers.models import Family, Parent, Child
from apps.courses.models import CourseType, Course
from apps.instructors.models import Instructor


def create_test_city(name="Test City"):
    """Create a test city"""
    return City.objects.create(name=name)


def create_test_branch(name="Test Branch", city=None):
    """Create a test branch"""
    if not city:
        city = create_test_city()
    return Branch.objects.create(
        name=name,
        city=city,
        address="123 Test St",
        phone="050-1234567"
    )


def create_test_instructor(first_name="John", last_name="Doe"):
    """Create a test instructor"""
    return Instructor.objects.create(
        first_name=first_name,
        last_name=last_name,
        phone="050-9876543",
        email="instructor@test.com"
    )


def create_test_course_type(name="Test Type"):
    """Create a test course type (תחום)"""
    return CourseType.objects.create(
        name=name,
        description="Test course type description",
        is_active=True,
    )


def create_test_course(
    name="Test Course",
    branch=None,
    instructor=None,  # deprecated - instructor is now lesson-specific
    price=250.00,
    course_type=None,
):
    """Create a test course"""
    if not course_type:
        course_type = create_test_course_type()
    if branch is None:
        branch = create_test_branch()

    return Course.objects.create(
        course_type=course_type,
        name=name,
        description="Test course description",
        price=price,
        capacity=20,
        branch=branch,
        min_age=5,
        max_age=12,
        is_active=True
    )


def create_test_family(name="Test Family", parent_id="123456789", branch=None):
    """Create a test family"""
    if not branch:
        branch = create_test_branch()
    
    return Family.objects.create(
        name=name,
        phone="050-1111111",
        email="family@test.com",
        address="456 Family St",
        parent_id_number=parent_id,
        branch=branch
    )


def create_test_parent(family=None, first_name="Parent", last_name="Test", is_primary=True):
    """Create a test parent"""
    if not family:
        family = create_test_family()
    
    return Parent.objects.create(
        family=family,
        first_name=first_name,
        last_name=last_name,
        phone="050-2222222",
        email="parent@test.com",
        is_primary=is_primary
    )


def create_test_child(
    family=None,
    first_name="Child",
    last_name="Test",
    birth_date=None,
    gender="male",
    id_number="987654321",
    status="trial",
    subscription_start_date=None,
    subscription_end_date=None,
    paid_until_date=None
):
    """Create a test child"""
    if not family:
        family = create_test_family()
    
    if not birth_date:
        # Default: 8 years old
        birth_date = date.today() - timedelta(days=8*365)
    
    return Child.objects.create(
        family=family,
        first_name=first_name,
        last_name=last_name,
        id_number=id_number,
        birth_date=birth_date,
        gender=gender,
        status=status,
        subscription_start_date=subscription_start_date,
        subscription_end_date=subscription_end_date,
        paid_until_date=paid_until_date
    )

