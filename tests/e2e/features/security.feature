Feature: Permissions and Security
  As the platform
  I must enforce authentication and role/object-level authorization
  So that users can only reach pages and data they are permitted to access

  Background:
    Given the teacher account exists in the database

  @smoke @security @sad-path
  Scenario Outline: Anonymous visitor cannot reach protected pages
    Given I am logged out
    When I navigate to the protected page "<path>"
    Then access should be blocked as unauthenticated

    Examples:
      | path                |
      | /teacher            |
      | /portal             |
      | /admin              |

  @security @sad-path
  Scenario: Student is denied the teacher dashboard
    Given the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And I am logged in as the student
    When I navigate to the protected page "/teacher"
    Then access should be forbidden
    And the page should not show the teacher dashboard

  @security @sad-path
  Scenario: Student is denied the admin area
    Given the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And I am logged in as the student
    When I navigate to the protected page "/admin"
    Then access should be forbidden

  @security @sad-path
  Scenario: Teacher is denied the admin area
    Given I am logged in as the teacher
    When I navigate to the protected page "/admin"
    Then access should be forbidden

  @security @sad-path
  Scenario: Teacher is denied the admin-only analytics dashboard
    Given I am logged in as the teacher
    When I navigate to the protected page "/teacher/analytics"
    Then access should be forbidden

  @security @sad-path
  Scenario: Teacher cannot open another teacher's subject
    Given another teacher owns a subject in the database
    And I am logged in as the teacher
    When I navigate to the other teacher's subject page
    Then access should be forbidden

  @security @sad-path
  Scenario: A bogus feedback token is rejected
    Given a bogus feedback token URL
    When I navigate to the bogus feedback token URL
    Then the feedback link should be rejected as not found

  @security @sad-path
  Scenario: A used feedback token cannot be reused
    Given the E2E test subject exists
    And the student is enrolled in the E2E test subject
    And a semester exists in the database
    And a feedback request exists for the E2E test subject
    And a feedback token URL is available
    And the feedback token has already been used
    When I navigate to the used feedback token URL
    Then the feedback link should be rejected as already used

  @security @sad-path
  Scenario: Logging out clears the session
    Given I am logged in as the teacher
    When I log out
    And I navigate to the protected page "/teacher"
    Then access should be blocked as unauthenticated
