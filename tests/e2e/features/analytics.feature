Feature: Analytics Dashboard
  As a logged-in teacher
  I want to view the analytics dashboard
  So that I can monitor student progress and submission statistics

  Background:
    Given the teacher account exists in the database
    And I am logged in as the teacher
    And the E2E test subject exists

  @smoke @analytics
  Scenario: Teacher views analytics dashboard without errors
    Given I am on the teacher dashboard
    When I navigate to the analytics page
    Then the analytics page should load without errors
    And statistical content should be visible on the page

  @analytics
  Scenario: Teacher views fraud detection analytics
    Given I am on the teacher dashboard
    When I navigate to the fraud analytics page
    Then the fraud analytics page should load without errors
